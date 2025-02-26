# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
# Copyright 2015 Abhijit Menon-Sen <ams@2ndQuadrant.com>
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
#
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import fcntl
import os
import pipes
import pty
import pwd
import select
import shlex
import subprocess
import time

from ansible import constants as C
from ansible.errors import AnsibleError, AnsibleConnectionFailure, AnsibleFileNotFound
from ansible.plugins.connection import ConnectionBase
from ansible.utils.path import unfrackpath, makedirs_safe

SSHPASS_AVAILABLE = None

class Connection(ConnectionBase):
    ''' ssh based connections '''

    transport = 'ssh'
    has_pipelining = True
    become_methods = frozenset(C.BECOME_METHODS).difference(['runas'])

    def __init__(self, *args, **kwargs):
        super(Connection, self).__init__(*args, **kwargs)

        self.host = self._play_context.remote_addr
        self.ssh_extra_args = ''
        self.ssh_args = ''

    def set_host_overrides(self, host):
        v = host.get_vars()
        if 'ansible_ssh_extra_args' in v:
            self.ssh_extra_args = v['ansible_ssh_extra_args']
        if 'ansible_ssh_args' in v:
            self.ssh_args = v['ansible_ssh_args']

    # The connection is created by running ssh/scp/sftp from the exec_command,
    # put_file, and fetch_file methods, so we don't need to do any connection
    # management here.

    def _connect(self):
        self._connected = True
        return self

    def close(self):
        # If we have a persistent ssh connection (ControlPersist), we can ask it
        # to stop listening. Otherwise, there's nothing to do here.

        # TODO: reenable once winrm issues are fixed
        # temporarily disabled as we are forced to currently close connections after every task because of winrm
        # if self._connected and self._persistent:
        #     cmd = self._build_command('ssh', '-O', 'stop', self.host)
        #     p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        #     stdout, stderr = p.communicate()

        self._connected = False

    def _build_command(self, binary, *other_args):
        '''
        Takes a binary (ssh, scp, sftp) and optional extra arguments and returns
        a command line as an array that can be passed to subprocess.Popen.
        '''

        self._command = []

        ## First, the command name.

        # If we want to use password authentication, we have to set up a pipe to
        # write the password to sshpass.

        if self._play_context.password:
            global SSHPASS_AVAILABLE

            # We test once if sshpass is available, and remember the result. It
            # would be nice to use distutils.spawn.find_executable for this, but
            # distutils isn't always available; shutils.which() is Python3-only.

            if SSHPASS_AVAILABLE is None:
                try:
                    p = subprocess.Popen(["sshpass"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    p.communicate()
                    SSHPASS_AVAILABLE = True
                except OSError:
                    SSHPASS_AVAILABLE = False

            if not SSHPASS_AVAILABLE:
                raise AnsibleError("to use the 'ssh' connection type with passwords, you must install the sshpass program")

            self.sshpass_pipe = os.pipe()
            self._command += ['sshpass', '-d{0}'.format(self.sshpass_pipe[0])]

        self._command += [binary]

        ## Next, additional arguments based on the configuration.

        # sftp batch mode allows us to correctly catch failed transfers, but can
        # be disabled if the client side doesn't support the option. FIXME: is
        # this still a real concern?

        if binary == 'sftp' and C.DEFAULT_SFTP_BATCH_MODE:
            self._command += ['-b', '-']

        elif binary == 'ssh':
            self._command += ['-C']

        if self._play_context.verbosity > 3:
            self._command += ['-vvv']
        elif binary == 'ssh':
            # Older versions of ssh (e.g. in RHEL 6) don't accept sftp -q.
            self._command += ['-q']

        # Next, we add ansible_ssh_args from the inventory if it's set, or
        # [ssh_connection]ssh_args from ansible.cfg, or the default Control*
        # settings.

        if self.ssh_args:
            args = self._split_args(self.ssh_args)
            self.add_args("inventory set ansible_ssh_args", args)
        elif C.ANSIBLE_SSH_ARGS:
            args = self._split_args(C.ANSIBLE_SSH_ARGS)
            self.add_args("ansible.cfg set ssh_args", args)
        else:
            args = (
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=60s"
            )
            self.add_args("default arguments", args)

        # Now we add various arguments controlled by configuration file settings
        # (e.g. host_key_checking) or inventory variables (ansible_ssh_port) or
        # a combination thereof.

        if not C.HOST_KEY_CHECKING:
            self.add_args(
                "ANSIBLE_HOST_KEY_CHECKING/host_key_checking disabled",
                ("-o", "StrictHostKeyChecking=no")
            )

        if self._play_context.port is not None:
            self.add_args(
                "ANSIBLE_REMOTE_PORT/remote_port/ansible_ssh_port set",
                ("-o", "Port={0}".format(self._play_context.port))
            )

        key = self._play_context.private_key_file
        if key:
            self.add_args(
                "ANSIBLE_PRIVATE_KEY_FILE/private_key_file/ansible_ssh_private_key_file set",
                ("-o", "IdentityFile=\"{0}\"".format(os.path.expanduser(key)))
            )

        if not self._play_context.password:
            self.add_args(
                "ansible_password/ansible_ssh_pass not set", (
                    "-o", "KbdInteractiveAuthentication=no",
                    "-o", "PreferredAuthentications=gssapi-with-mic,gssapi-keyex,hostbased,publickey",
                    "-o", "PasswordAuthentication=no"
                )
            )

        user = self._play_context.remote_user
        if user and user != pwd.getpwuid(os.geteuid())[0]:
            self.add_args(
                "ANSIBLE_REMOTE_USER/remote_user/ansible_ssh_user/user/-u set",
                ("-o", "User={0}".format(self._play_context.remote_user))
            )

        self.add_args(
            "ANSIBLE_TIMEOUT/timeout set",
            ("-o", "ConnectTimeout={0}".format(self._play_context.timeout))
        )

        # If any extra SSH arguments are specified in the inventory for
        # this host, or specified as an override on the command line,
        # add them in.

        if self._play_context.ssh_extra_args:
            args = self._split_args(self._play_context.ssh_extra_args)
            self.add_args("command-line added --ssh-extra-args", args)
        elif self.ssh_extra_args:
            args = self._split_args(self.ssh_extra_args)
            self.add_args("inventory added ansible_ssh_extra_args", args)

        # If ssh_args or ssh_extra_args set ControlPersist but not a
        # ControlPath, add one ourselves.

        cp_in_use = False
        cp_path_set = False
        for arg in self._command:
            if "ControlPersist" in arg:
                cp_in_use = True
            if "ControlPath" in arg:
                cp_path_set = True

        if cp_in_use and not cp_path_set:
            self._cp_dir = unfrackpath('$HOME/.ansible/cp')

            args = ("-o", "ControlPath={0}".format(
                C.ANSIBLE_SSH_CONTROL_PATH % dict(directory=self._cp_dir))
            )
            self.add_args("found only ControlPersist; added ControlPath", args)

            # The directory must exist and be writable.
            makedirs_safe(self._cp_dir, 0o700)
            if not os.access(self._cp_dir, os.W_OK):
                raise AnsibleError("Cannot write to ControlPath %s" % self._cp_dir)

        # If the configuration dictates that we use a persistent connection,
        # then we remember that for later. (We could be more thorough about
        # detecting this, though.)

        if cp_in_use:
            self._persistent = True

        ## Finally, we add any caller-supplied extras.

        if other_args:
            self._command += other_args

        return self._command

    def exec_command(self, *args, **kwargs):
        """
        Wrapper around _exec_command to retry in the case of an ssh failure

        Will retry if:
        * an exception is caught
        * ssh returns 255
        Will not retry if
        * remaining_tries is <2
        * retries limit reached
        """

        remaining_tries = int(C.ANSIBLE_SSH_RETRIES) + 1
        cmd_summary = "%s..." % args[0]
        for attempt in xrange(remaining_tries):
            try:
                return_tuple = self._exec_command(*args, **kwargs)
                # 0 = success
                # 1-254 = remote command return code
                # 255 = failure from the ssh command itself
                if return_tuple[0] != 255 or attempt == (remaining_tries - 1):
                    break
                else:
                    raise AnsibleConnectionFailure("Failed to connect to the host via ssh.")
            except (AnsibleConnectionFailure, Exception) as e:
                if attempt == remaining_tries - 1:
                    raise e
                else:
                    pause = 2 ** attempt - 1
                    if pause > 30:
                        pause = 30

                    if isinstance(e, AnsibleConnectionFailure):
                        msg = "ssh_retry: attempt: %d, ssh return code is 255. cmd (%s), pausing for %d seconds" % (attempt, cmd_summary, pause)
                    else:
                        msg = "ssh_retry: attempt: %d, caught exception(%s) from cmd (%s), pausing for %d seconds" % (attempt, e, cmd_summary, pause)

                    self._display.vv(msg)

                    time.sleep(pause)
                    continue

        return return_tuple

    def _exec_command(self, cmd, in_data=None, sudoable=True):
        ''' run a command on the remote host '''

        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        self._display.vvv("ESTABLISH SSH CONNECTION FOR USER: {0}".format(self._play_context.remote_user), host=self._play_context.remote_addr)

        # we can only use tty when we are not pipelining the modules. piping
        # data into /usr/bin/python inside a tty automatically invokes the
        # python interactive-mode but the modules are not compatible with the
        # interactive-mode ("unexpected indent" mainly because of empty lines)

        if in_data:
            cmd = self._build_command('ssh', self.host, cmd)
        else:
            cmd = self._build_command('ssh', '-tt', self.host, cmd)

        (returncode, stdout, stderr) = self._run(cmd, in_data, sudoable=sudoable)

        return (returncode, stdout, stderr)

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''

        super(Connection, self).put_file(in_path, out_path)

        self._display.vvv("PUT {0} TO {1}".format(in_path, out_path), host=self.host)
        if not os.path.exists(in_path):
            raise AnsibleFileNotFound("file or module does not exist: {0}".format(in_path))

        # scp and sftp require square brackets for IPv6 addresses, but
        # accept them for hostnames and IPv4 addresses too.
        host = '[%s]' % self.host

        if C.DEFAULT_SCP_IF_SSH:
            cmd = self._build_command('scp', in_path, '{0}:{1}'.format(host, pipes.quote(out_path)))
            in_data = None
        else:
            cmd = self._build_command('sftp', host)
            in_data = "put {0} {1}\n".format(pipes.quote(in_path), pipes.quote(out_path))

        (returncode, stdout, stderr) = self._run(cmd, in_data)

        if returncode != 0:
            raise AnsibleError("failed to transfer file to {0}:\n{1}\n{2}".format(out_path, stdout, stderr))

    def fetch_file(self, in_path, out_path):
        ''' fetch a file from remote to local '''

        super(Connection, self).fetch_file(in_path, out_path)

        self._display.vvv("FETCH {0} TO {1}".format(in_path, out_path), host=self.host)

        # scp and sftp require square brackets for IPv6 addresses, but
        # accept them for hostnames and IPv4 addresses too.
        host = '[%s]' % self.host

        if C.DEFAULT_SCP_IF_SSH:
            cmd = self._build_command('scp', '{0}:{1}'.format(host, pipes.quote(in_path)), out_path)
            in_data = None
        else:
            cmd = self._build_command('sftp', host)
            in_data = "get {0} {1}\n".format(pipes.quote(in_path), pipes.quote(out_path))

        (returncode, stdout, stderr) = self._run(cmd, in_data)

        if returncode != 0:
            raise AnsibleError("failed to transfer file from {0}:\n{1}\n{2}".format(in_path, stdout, stderr))

    def _run(self, cmd, in_data, sudoable=True):
        '''
        Starts the command and communicates with it until it ends.
        '''

        display_cmd = map(pipes.quote, cmd[:-1]) + [cmd[-1]]
        self._display.vvv('SSH: EXEC {0}'.format(' '.join(display_cmd)), host=self.host)

        # Start the given command. If we don't need to pipeline data, we can try
        # to use a pseudo-tty (ssh will have been invoked with -tt). If we are
        # pipelining data, or can't create a pty, we fall back to using plain
        # old pipes.

        p = None
        if not in_data:
            try:
                # Make sure stdin is a proper pty to avoid tcgetattr errors
                master, slave = pty.openpty()
                p = subprocess.Popen(cmd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdin = os.fdopen(master, 'w', 0)
                os.close(slave)
            except (OSError, IOError):
                p = None

        if not p:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdin = p.stdin

        # If we are using SSH password authentication, write the password into
        # the pipe we opened in _build_command.

        if self._play_context.password:
            os.close(self.sshpass_pipe[0])
            os.write(self.sshpass_pipe[1], "{0}\n".format(self._play_context.password))
            os.close(self.sshpass_pipe[1])

        ## SSH state machine
        #
        # Now we read and accumulate output from the running process until it
        # exits. Depending on the circumstances, we may also need to write an
        # escalation password and/or pipelined input to the process.

        states = [
            'awaiting_prompt', 'awaiting_escalation', 'ready_to_send', 'awaiting_exit'
        ]

        # Are we requesting privilege escalation? Right now, we may be invoked
        # to execute sftp/scp with sudoable=True, but we can request escalation
        # only when using ssh. Otherwise we can send initial data straightaway.

        state = states.index('ready_to_send')
        if 'ssh' in cmd:
            if self._play_context.prompt:
                # We're requesting escalation with a password, so we have to
                # wait for a password prompt.
                state = states.index('awaiting_prompt')
                self._display.debug('Initial state: %s: %s' % (states[state], self._play_context.prompt))
            elif self._play_context.become and self._play_context.success_key:
                # We're requesting escalation without a password, so we have to
                # detect success/failure before sending any initial data.
                state = states.index('awaiting_escalation')
                self._display.debug('Initial state: %s: %s' % (states[state], self._play_context.success_key))

        # We store accumulated stdout and stderr output from the process here,
        # but strip any privilege escalation prompt/confirmation lines first.
        # Output is accumulated into tmp_*, complete lines are extracted into
        # an array, then checked and removed or copied to stdout or stderr. We
        # set any flags based on examining the output in self._flags.

        stdout = stderr = ''
        tmp_stdout = tmp_stderr = ''

        self._flags = dict(
            become_prompt=False, become_success=False,
            become_error=False, become_nopasswd_error=False
        )

        timeout = self._play_context.timeout
        rpipes = [p.stdout, p.stderr]
        for fd in rpipes:
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)

        # If we can send initial data without waiting for anything, we do so
        # before we call select.

        if states[state] == 'ready_to_send' and in_data:
            self._send_initial_data(stdin, in_data)
            state += 1

        while True:
            rfd, wfd, efd = select.select(rpipes, [], [], timeout)

            # We pay attention to timeouts only while negotiating a prompt.

            if not rfd:
                if state <= states.index('awaiting_escalation'):
                    self._terminate_process(p)
                    raise AnsibleError('Timeout (%ds) waiting for privilege escalation prompt: %s' % (timeout, stdout))

            # Read whatever output is available on stdout and stderr, and stop
            # listening to the pipe if it's been closed.

            if p.stdout in rfd:
                chunk = p.stdout.read()
                if chunk == '':
                    rpipes.remove(p.stdout)
                tmp_stdout += chunk
                self._display.debug("stdout chunk (state=%s):\n>>>%s<<<\n" % (state, chunk))

            if p.stderr in rfd:
                chunk = p.stderr.read()
                if chunk == '':
                    rpipes.remove(p.stderr)
                tmp_stderr += chunk
                self._display.debug("stderr chunk (state=%s):\n>>>%s<<<\n" % (state, chunk))

            # We examine the output line-by-line until we have negotiated any
            # privilege escalation prompt and subsequent success/error message.
            # Afterwards, we can accumulate output without looking at it.

            if state < states.index('ready_to_send'):
                if tmp_stdout:
                    output, unprocessed = self._examine_output('stdout', states[state], tmp_stdout, sudoable)
                    stdout += output
                    tmp_stdout = unprocessed

                if tmp_stderr:
                    output, unprocessed = self._examine_output('stderr', states[state], tmp_stderr, sudoable)
                    stderr += output
                    tmp_stderr = unprocessed
            else:
                stdout += tmp_stdout
                stderr += tmp_stderr
                tmp_stdout = tmp_stderr = ''

            # If we see a privilege escalation prompt, we send the password.

            if states[state] == 'awaiting_prompt' and self._flags['become_prompt']:
                self._display.debug('Sending become_pass in response to prompt')
                stdin.write(self._play_context.become_pass + '\n')
                self._flags['become_prompt'] = False
                state += 1

            # We've requested escalation (with or without a password), now we
            # wait for an error message or a successful escalation.

            if states[state] == 'awaiting_escalation':
                if self._flags['become_success']:
                    self._display.debug('Escalation succeeded')
                    self._flags['become_success'] = False
                    state += 1
                elif self._flags['become_error']:
                    self._display.debug('Escalation failed')
                    self._terminate_process(p)
                    self._flags['become_error'] = False
                    raise AnsibleError('Incorrect %s password' % self._play_context.become_method)
                elif self._flags['become_nopasswd_error']:
                    self._display.debug('Escalation requires password')
                    self._terminate_process(p)
                    self._flags['become_nopasswd_error'] = False
                    raise AnsibleError('Missing %s password' % self._play_context.become_method)
                elif self._flags['become_prompt']:
                    # This shouldn't happen, because we should see the "Sorry,
                    # try again" message first.
                    self._display.debug('Escalation prompt repeated')
                    self._terminate_process(p)
                    self._flags['become_prompt'] = False
                    raise AnsibleError('Incorrect %s password' % self._play_context.become_method)

            # Once we're sure that the privilege escalation prompt, if any, has
            # been dealt with, we can send any initial data and start waiting
            # for output.

            if states[state] == 'ready_to_send':
                if in_data:
                    self._send_initial_data(stdin, in_data)
                state += 1

            # Now we're awaiting_exit: has the child process exited? If it has,
            # and we've read all available output from it, we're done.

            if p.poll() is not None:
                if not rpipes or not rfd:
                    break

                # When ssh has ControlMaster (+ControlPath/Persist) enabled, the
                # first connection goes into the background and we never see EOF
                # on stderr. If we see EOF on stdout and the process has exited,
                # we're probably done. We call select again with a zero timeout,
                # just to make certain we don't miss anything that may have been
                # written to stderr between the time we called select() and when
                # we learned that the process had finished.

                if not p.stdout in rpipes:
                    timeout = 0
                    continue

            # If the process has not yet exited, but we've already read EOF from
            # its stdout and stderr (and thus removed both from rpipes), we can
            # just wait for it to exit.

            elif not rpipes:
                p.wait()
                break

            # Otherwise there may still be outstanding data to read.

        # close stdin after process is terminated and stdout/stderr are read
        # completely (see also issue #848)
        stdin.close()

        if C.HOST_KEY_CHECKING:
            if cmd[0] == "sshpass" and p.returncode == 6:
                raise AnsibleError('Using a SSH password instead of a key is not possible because Host Key checking is enabled and sshpass does not support this.  Please add this host\'s fingerprint to your known_hosts file to manage this host.')

        controlpersisterror = 'Bad configuration option: ControlPersist' in stderr or 'unknown configuration option: ControlPersist' in stderr
        if p.returncode != 0 and controlpersisterror:
            raise AnsibleError('using -c ssh on certain older ssh versions may not support ControlPersist, set ANSIBLE_SSH_ARGS="" (or ssh_args in [ssh_connection] section of the config file) before running again')

        if p.returncode == 255 and in_data:
            raise AnsibleConnectionFailure('SSH Error: data could not be sent to the remote host. Make sure this host can be reached over ssh')

        return (p.returncode, stdout, stderr)

    def _send_initial_data(self, fh, in_data):
        '''
        Writes initial data to the stdin filehandle of the subprocess and closes
        it. (The handle must be closed; otherwise, for example, "sftp -b -" will
        just hang forever waiting for more commands.)
        '''

        self._display.debug('Sending initial data')

        try:
            fh.write(in_data)
            fh.close()
        except (OSError, IOError):
            raise AnsibleConnectionFailure('SSH Error: data could not be sent to the remote host. Make sure this host can be reached over ssh')

        self._display.debug('Sent initial data (%d bytes)' % len(in_data))

    # This is a separate method because we need to do the same thing for stdout
    # and stderr.

    def _examine_output(self, source, state, chunk, sudoable):
        '''
        Takes a string, extracts complete lines from it, tests to see if they
        are a prompt, error message, etc., and sets appropriate flags in self.
        Prompt and success lines are removed.

        Returns the processed (i.e. possibly-edited) output and the unprocessed
        remainder (to be processed with the next chunk) as strings.
        '''

        output = []
        for l in chunk.splitlines(True):
            suppress_output = False

            # self._display.debug("Examining line (source=%s, state=%s): '%s'" % (source, state, l.rstrip('\r\n')))
            if self._play_context.prompt and self.check_password_prompt(l):
                self._display.debug("become_prompt: (source=%s, state=%s): '%s'" % (source, state, l.rstrip('\r\n')))
                self._flags['become_prompt'] = True
                suppress_output = True
            elif self._play_context.success_key and self.check_become_success(l):
                self._display.debug("become_success: (source=%s, state=%s): '%s'" % (source, state, l.rstrip('\r\n')))
                self._flags['become_success'] = True
                suppress_output = True
            elif sudoable and self.check_incorrect_password(l):
                self._display.debug("become_error: (source=%s, state=%s): '%s'" % (source, state, l.rstrip('\r\n')))
                self._flags['become_error'] = True
            elif sudoable and self.check_missing_password(l):
                self._display.debug("become_nopasswd_error: (source=%s, state=%s): '%s'" % (source, state, l.rstrip('\r\n')))
                self._flags['become_nopasswd_error'] = True

            if not suppress_output:
                output.append(l)

        # The chunk we read was most likely a series of complete lines, but just
        # in case the last line was incomplete (and not a prompt, which we would
        # have removed from the output), we retain it to be processed with the
        # next chunk.

        remainder = ''
        if output and not output[-1].endswith('\n'):
            remainder = output[-1]
            output = output[:-1]

        return ''.join(output), remainder

    # Utility functions

    def _terminate_process(self, p):
        try:
            p.terminate()
        except (OSError, IOError):
            pass

    def _split_args(self, argstring):
        """
        Takes a string like '-o Foo=1 -o Bar="foo bar"' and returns a
        list ['-o', 'Foo=1', '-o', 'Bar=foo bar'] that can be added to
        the argument list. The list will not contain any empty elements.
        """
        return [x.strip() for x in shlex.split(argstring) if x.strip()]

    def add_args(self, explanation, args):
        """
        Adds the given args to self._command and displays a caller-supplied
        explanation of why they were added.
        """
        self._command += args
        self._display.vvvvv('SSH: ' + explanation + ': (%s)' % ')('.join(args), host=self._play_context.remote_addr)
