"""
Refactoring of python-varnish library for Varnish 4

Tested against
    Varnish v4.*
    Varnish Cache CLI 1.0

Supports the following commands
ban <field> <operator> <arg> [&& <field> <oper> <arg>]...

Also VarnishManager.purge will do HTTP purges. See below for
configuration details

https://www.varnish-cache.org/docs/3.0/tutorial/purging.html

"""
import sys
from telnetlib import Telnet
from threading import Thread
from hashlib import sha256

try:
    from httplib import HTTPConnection
except ImportError:
    from http.client import HTTPConnection

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

PYTHON3=True if sys.version_info[0] == 3 else False

def http_purge_url(url):
    """
    Do an HTTP PURGE of the given asset.
    The URL is run through urlparse and must point
    to the varnish instance not the varnishadm
    """
    url = urlparse(url)
    connection = HTTPConnection(url.hostname, url.port or 80)
    connection.request('PURGE', '%s?%s' % (url.path or '/', url.query), '',
                       {'Host': url.hostname})
    response = connection.getresponse()
    return response


class VarnishHandler(Telnet):
    def __init__(self, host_port_timeout, secret=None, **kwargs):
        self.sock = None
        if isinstance(host_port_timeout, str):
            host_port_timeout = host_port_timeout.split(':')
        Telnet.__init__(self, *host_port_timeout)
        (status, length), content = self._read()
        if status == 107 and secret is not None:
            self.auth(secret, content)

    def _read(self):
        if PYTHON3:
            (status, length), content = list(map(int, self.read_until(b'\n').split())), b''
        else:
            (status, length), content = list(map(int, self.read_until('\n').split())), ''
        while len(content) < length:
            content += self.read_some()
        return (status, length), content[:-1]

    def fetch(self, command):
        """
        Run a command on the Varnish backend and return the result
        return value is a tuple of ((status, length), content)
        """
        if PYTHON3:
            self.write(bytes('%s\n' % command, 'utf8'))
        else:
            self.write('%s\n' % command)
        while 1:
            if PYTHON3:
                buffer = self.read_until(b'\n').strip()
            else:
                buffer = self.read_until('\n').strip()
            if len(buffer):
                break
        status, length = map(int, buffer.split())
        if PYTHON3:
            content = b''
        else:
            content = ''

        if status != 200:
            if PYTHON3:
                raise VarnishError(status, 'Bad response code: {status} {text} ({command})'.format(status=status, text=self.read_until(b'\n').strip().decode('utf8'), command=command))
            else:
                raise VarnishError(status, 'Bad response code: {status} {text} ({command})'.format(status=status, text=self.read_until(b'\n').strip(), command=command))
        while len(content) < length:
            if PYTHON3:
                content += self.read_until(b'\n')
            else:
                content += self.read_until('\n')
        if PYTHON3:
            content = content.decode('utf8')
        self.read_eager()
        return (status, length), content

    # Service control methods
    def start(self):
        """
        start  Start the Varnish cache process
        if it is not already running.
        """
        return self.fetch('start')

    def stop(self):
        """stop   Stop the Varnish cache process."""
        return self.fetch('stop')

    def quit(self):
        """quit   Close the connection to the varnish admin port."""
        return self.close()

    def auth(self, secret, content):
        challenge = content[:32]
        if PYTHON3:
            challenge_resp = challenge + b'\n' + bytes(secret, 'utf8') + b'\n' + challenge + b'\n'
        else:
            challenge_resp = '%s\n%s%s\n' % (challenge, secret, challenge)
        response = sha256(challenge_resp)
        response_str = 'auth %s' % response.hexdigest()

        try:
            self.fetch(response_str)
        except VarnishError as e:
            if e.status == 107:
                raise VarnishSecretInvalidError(107, "Invalid secret")
            raise e

    # Information methods
    def ping(self, timestamp=None):
        """
        ping [timestamp]
            Ping the Varnish cache process, keeping the connection alive.
        """
        cmd = 'ping'
        if timestamp:
            cmd += ' %s' % timestamp
        return tuple(map(float, self.fetch(cmd)[1].split()[1:]))

    def status(self):
        """status Check the status of the Varnish cache process."""
        return self.fetch('status')[1]

    def help(self, command=None):
        """
        help [command]
            Display a list of available commands.
            If the command is specified, display help for this command.
        """
        cmd = 'help'
        if command:
            cmd += ' %s' % command
        return self.fetch(cmd)[1]

    # VCL methods
    def vcl_load(self, configname, filename):
        """
        vcl.load configname filename
            Create a new configuration named configname
            with the contents of the specified file.
        """
        return self.fetch('vcl.load %s %s' % (configname, filename))

    def vcl_inline(self, configname, vclcontent):
        """
        vcl.inline configname vcl
            Create a new configuration named configname with
            the VCL code specified by vcl, which must be  a
            quoted string.
        """
        return self.fetch('vcl.inline %s %s' % (configname, vclcontent))

    def vcl_show(self, configname):
        """
        vcl.show configname
            Display the source code for the specified configuration.
        """
        return self.fetch('vcl.show' % configname)

    def vcl_use(self, configname):
        """
        vcl.use configname
            Start using the configuration specified by configname
            for all new requests.   Existing  requests
            will coninue using whichever configuration was in
            use when they arrived.
        """
        return self.fetch('vcl.use %s' % configname)

    def vcl_discard(self, configname):
        """
        vcl.discard configname
            Discard  the  configuration  specified by configname.
            This will have no effect if the specified
            configuration has a non-zero reference count.
        """
        return self.fetch('vcl.discard %s' % configname)

    def vcl_list(self):
        """
        vcl.list
            List  available  configurations and their respective
            reference counts.  The active configuration
            is indicated with an asterisk ("*").
        """
        vcls = {}
        for line in self.fetch('vcl.list')[1].splitlines():
            a = line.split()
            vcls[a[2]] = tuple(a[:-1])
        return vcls

    # Param methods
    def param_show(self, param, l=False):
        """
        param.show [-l] [param]
              Display a list if run-time parameters and their values.
              If the -l option is specified, the list includes a brief
              explanation of each parameter.
              If a param is specified, display only the value and
              explanation for this parameter.
        """
        cmd = 'param.show '
        if l:
            cmd += '-l '
        return self.fetch(cmd + param)

    def param_set(self, param, value):
        """
        param.set param value
              Set the parameter specified by param to the specified
              value.  See Run-Time Parameters for a list
              of paramea ters.
        """
        self.fetch('param.set %s %s' % (param, value))

    # Ban methods
    def ban(self, expression):
        """
        ban field operator argument [&& field operator argument [...]]
            Immediately invalidate all documents matching the ban expression.
            See Ban Expressions for  more
            documentation and examples.
        """
        return self.fetch('ban %s' % expression)[1]

    def ban_url(self, regex):
        """
        ban.url regexp
            Immediately invalidate all documents whose URL matches the
            specified regular expression.  Please
            note  that the Host part of the URL is ignored, so if
            you have several virtual hosts all of them
            will be banned. Use ban to specify a complete ban if
            you need to narrow it down.
        """
        return self.fetch('ban req.url ~ %s' % regex)[1]

    def ban_list(self):
        """
        ban.list
            All requests for objects from the cache are matched
            against items on the ban list.  If an object
            in the cache is older than a matching ban list
            item, it is  considered  "banned",  and  will  be
            fetched from the backend instead.

            When a ban expression is older than all the objects
            in the cache, it is removed from the list.

            ban.list displays the ban list. The output looks
            something like this (broken into two lines):

            0x7fea4fcb0580 1303835108.618863   131G
            req.http.host ~ www.myhost.com && req.url ~ /some/url

            The first field is the address of the ban.

            The second is the time of entry into the list, given
            as a high precision timestamp.

            The  third  field  describes many objects point to this
            ban. When an object is compared to a ban
            the object is marked with a reference to the newest
            ban it was tested against. This isn't really
            useful unless you're debugging.

            A "G" marks that the ban is "Gone". Meaning it has been
            marked as a duplicate or it is no longer
            valid. It stays in the list for effiency reasons.

            Then follows the actual ban it self.
        """
        return self.fetch('ban.list')[1]

    def purge_url(self, url):
        """
        Wrapper for http_purge_url
        """
        return http_purge_url(url)


class ThreadedRunner(Thread):
    """
    Runs commands on a particular varnish server in a separate thread
    """
    def __init__(self, addr, *commands, **kwargs):
        self.addr = addr
        self.commands = commands
        self.kwargs = kwargs
        super(ThreadedRunner, self).__init__()

    def run(self):
        handler = VarnishHandler(self.addr, **self.kwargs)
        for cmd in self.commands:
            if isinstance(cmd, tuple) and len(cmd) > 1:
                getattr(handler, cmd[0].replace('.', '_'))(*cmd[1:])
            else:
                getattr(handler, cmd.replace('.', '_'))()
        handler.close()


def run(addr, *commands, **kwargs):
    """
    Non-threaded batch command runner returning output results
    """
    results = []
    handler = VarnishHandler(addr, **kwargs)
    for cmd in commands:
        if isinstance(cmd, tuple) and len(cmd) > 1:
            results.extend([getattr(handler, c[0]
                           .replace('.', '_'))(*c[1:]) for c in cmd])
        else:
            results.append(getattr(handler,
                           cmd.replace('.', '_'))(*commands[1:]))
            break
    handler.close()
    return results


class VarnishManager(object):
    def __init__(self, servers, secret=None):
        self.servers = servers
        self.secret = secret

    def run(self, *commands, **kwargs):
        threaded = kwargs.pop('threaded', False)
        if 'secret' not in kwargs and self.secret is not None:
            kwargs['secret'] = self.secret
        for server in self.servers:
            if threaded:
                [ThreadedRunner(server, *commands, **kwargs).start()
                    for server in self.servers]
            else:
                return [run(server, *commands, **kwargs)
                        for server in self.servers]

    def help(self, *args):
        return run(self.servers[0], *('help',)+args)[0]

    def close(self):
        self.run('close', threaded=True)
        self.servers = ()


class VarnishError(RuntimeError):

    def __init__(self, status, message, *args, **kwargs):
        self.status = status
        self.message = message
        super(VarnishError, self).__init__(message, *args, **kwargs)

class VarnishSecretInvalidError(VarnishError):
    pass
