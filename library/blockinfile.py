#!/usr/bin/python
# -*- coding: utf-8 -*-

# (c) 2014, 2015 YAEGASHI Takeshi <yaegashi@debian.org>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import re
import os
import tempfile

from itertools import chain

import pprint

DOCUMENTATION = """
---
module: blockinfile
author:
    - 'YAEGASHI Takeshi (@yaegashi)'
extends_documentation_fragment:
    - files
    - validate
short_description: Insert/update/remove a text block
                   surrounded by marker lines.
version_added: '2.0'
description:
  - This module will insert/update/remove a block of multi-line text
    surrounded by customizable marker lines.
notes:
  - This module supports check mode.
options:
  dest:
    aliases: [ name, destfile ]
    required: true
    description:
      - The file to modify.
  state:
    required: false
    choices: [ present, absent ]
    default: present
    description:
      - Whether the block should be there or not.
  marker:
    required: false
    default: '# {mark} ANSIBLE MANAGED BLOCK'
    description:
      - The marker line template.
        "{mark}" will be replaced with "BEGIN" or "END".
  block:
    aliases: [ content ]
    required: false
    default: ''
    description:
      - The text to insert inside the marker lines.
        If it's missing or an empty string,
        the block will be removed as if C(state) were specified to C(absent).
  insertafter:
    required: false
    default: EOF
    description:
      - If specified, the block will be inserted after the last match of
        specified regular expression. A special value is available; C(EOF) for
        inserting the block at the end of the file.  If specified regular
        expresion has no matches, C(EOF) will be used instead.
    choices: [ 'EOF', '*regex*' ]
  insertbefore:
    required: false
    default: None
    description:
      - If specified, the block will be inserted before the last match of
        specified regular expression. A special value is available; C(BOF) for
        inserting the block at the beginning of the file.  If specified regular
        expresion has no matches, the block will be inserted at the end of the
        file.
    choices: [ 'BOF', '*regex*' ]
  create:
    required: false
    default: 'no'
    choices: [ 'yes', 'no' ]
    description:
      - Create a new file if it doesn't exist.
  backup:
    required: false
    default: 'no'
    choices: [ 'yes', 'no' ]
    description:
      - Create a backup file including the timestamp information so you can
        get the original file back if you somehow clobbered it incorrectly.
"""

EXAMPLES = r"""
- name: insert/update "Match User" configuation block in /etc/ssh/sshd_config
  blockinfile:
    dest: /etc/ssh/sshd_config
    block: |
      Match User ansible-agent
      PasswordAuthentication no

- name: insert/update eth0 configuration stanza in /etc/network/interfaces
        (it might be better to copy files into /etc/network/interfaces.d/)
  blockinfile:
    dest: /etc/network/interfaces
    block: |
      iface eth0 inet static
          address 192.168.0.1
          netmask 255.255.255.0

- name: insert/update HTML surrounded by custom markers after <body> line
  blockinfile:
    dest: /var/www/html/index.html
    marker: "<!-- {mark} ANSIBLE MANAGED BLOCK -->"
    insertafter: "<body>"
    content: |
      <h1>Welcome to {{ansible_hostname}}</h1>
      <p>Last updated on {{ansible_date_time.iso8601}}</p>

- name: remove HTML as well as surrounding markers
  blockinfile:
    dest: /var/www/html/index.html
    marker: "<!-- {mark} ANSIBLE MANAGED BLOCK -->"
    content: ""
"""


def write_changes(module, contents, dest):

    tmpfd, tmpfile = tempfile.mkstemp()
    f = os.fdopen(tmpfd, 'wb')
    f.write(contents)
    f.close()

    validate = module.params.get('validate', None)
    valid = not validate
    if validate:
        if "%s" not in validate:
            module.fail_json(msg="validate must contain %%s: %s" % (validate))
        (rc, out, err) = module.run_command(validate % tmpfile)
        valid = rc == 0
        if rc != 0:
            module.fail_json(msg='failed to validate: '
                                 'rc:%s error:%s' % (rc, err))
    if valid:
        module.atomic_move(tmpfile, dest)


def check_file_attrs(module, changed, message):

    file_args = module.load_file_common_arguments(module.params)
    if module.set_file_attributes_if_different(file_args, False):

        if changed:
            message += " and "
        changed = True
        message += "ownership, perms or SE linux context changed"

    return message, changed


def startswith_lines(marker, lines, index):
    for i in range(0, len(marker)):
        if lines[i] == marker[0 + index]:
            pass
        else:
            return False
    return True


def main():
    module = AnsibleModule(
        argument_spec=dict(
            dest=dict(required=True, aliases=['name', 'destfile']),
            state=dict(default='present', choices=['absent', 'present']),
            marker=dict(default='# {mark} ANSIBLE MANAGED BLOCK', type='str'),
            block=dict(default='', type='str', aliases=['content']),
            insertafter=dict(default=None),
            insertbefore=dict(default=None),
            create=dict(default=False, type='bool'),
            backup=dict(default=False, type='bool'),
            validate=dict(default=None, type='str'),
            beginmarker=dict(default=None, type='str'),
            endmarker=dict(default=None, type='str'),
        ),
        mutually_exclusive=[
            ['insertbefore', 'insertafter'],
            ['block', 'src'],
        ],
        add_file_common_args=True,
        supports_check_mode=True
    )

    params = module.params
    dest = os.path.expanduser(params['dest'])
    if module.boolean(params.get('follow', None)):
        dest = os.path.realpath(dest)

    if os.path.isdir(dest):
        module.fail_json(rc=256,
                         msg='Destination %s is a directory !' % dest)

    if not os.path.exists(dest):
        if not module.boolean(params['create']):
            module.fail_json(rc=257,
                             msg='Destination %s does not exist !' % dest)
        original = ''
        lines = []
    else:
        f = open(dest, 'rb')
        original = f.read()
        f.close()
        lines = original.splitlines()

    insertbefore = params['insertbefore']
    insertafter = params['insertafter']
    
    # accept `src` as a file for the block contents
    if params['src']:
        srcFile = open(params['src'], 'rb')
        block = srcFile.read()
        srcFile.close()
    else:
        block = params['block']
    
    marker = params['marker']
    present = params['state'] == 'present'

    if insertbefore is None and insertafter is None:
        insertafter = 'EOF'

    if insertafter not in (None, 'EOF'):
        insertre = re.compile(insertafter)
    elif insertbefore not in (None, 'BOF'):
        insertre = re.compile(insertbefore)
    else:
        insertre = None
    
    if params['beginmarker']:
        marker0 = params['beginmarker']
    else:
        marker0 = re.sub(r'{mark}', 'BEGIN', marker)
    
    if params['endmarker']:
        marker1 = params['endmarker']
    else:
        marker1 = re.sub(r'{mark}', 'END', marker)
    
    if present and block:
        # Escape seqeuences like '\n' need to be handled in Ansible 1.x
        if module.ansible_version.startswith('1.'):
            block = re.sub('', block, '')
            marker0 = re.sub('', marker0, '')
            marker1 = re.sub('', marker1, '')
    else:
        pass
    
    # make sure each chunk ends with a newline if it doens't already
    if block != '' and not block.endswith("\n"):
        block = block + "\n"
        
    if not marker0.endswith("\n"):
        marker0 = marker0 + "\n"
        
    if not marker1.endswith("\n"):
        marker1 = marker1 + "\n"
    
    replacement = marker0 + block + marker1
    
    # module.fail_json(msg=pprint.pformat(replacement))

    exact_re = re.compile(
        re.escape(marker0 + block + marker1),
        (re.MULTILINE | re.DOTALL)
    )
    
    # this regex will match if the markers are present but the contents is
    # different
    different_re = re.compile(
        "%s.*%s" % (re.escape(marker0), re.escape(marker1)),
        (re.MULTILINE | re.DOTALL)
    )
    
    result = original
    
    # there are four cases:
    # 
    # 1.  removal - this is it's own case because the markers are
    #     removed along with the content.
    # 
    if not present or block == '':
        result = different_re.sub('', original)
    
    # 2.  no-op - the exact text is already present
    elif exact_re.search(original):
        pass
        
    # 3. replace - the markers are present but the content is different
    elif different_re.search(original):
        result = different_re.sub(replacement, original)
    
    # 4.  insert - the markers 
    else:
        lines = original.splitlines()
        
        n0 = None
        if insertre is not None:
            match = insertre.search(original)    
            for i, line in enumerate(lines):
                if insertre.search(line):
                    n0 = i
            if n0 is None:
                n0 = len(lines)
            elif insertafter is not None:
                n0 += 1
        elif insertbefore is not None:
            n0 = 0           # insertbefore=BOF
        else:
            n0 = len(lines)  # insertafter=EOF
        
        before = "\n".join(lines[:n0])
        
        if before != '':
            before = before + "\n"
        
        after = "\n".join(lines[n0:])
        
        if after != '' and original.endswith("\n"):
            after = after + "\n"
        
        result = "".join([before, replacement, after])
                
    if original == result:
        msg = ''
        changed = False
    elif original == '':
        msg = 'File created'
        changed = True
    elif not block:
        msg = 'Block removed'
        changed = True
    else:
        msg = 'Block inserted'
        changed = True

    if changed and not module.check_mode:
        if module.boolean(params['backup']) and os.path.exists(dest):
            module.backup_local(dest)
        write_changes(module, result, dest)

    msg, changed = check_file_attrs(module, changed, msg)
    module.exit_json(changed=changed, msg=msg)

# import module snippets
from ansible.module_utils.basic import *
from ansible.module_utils.splitter import *
if __name__ == '__main__':
    main()
