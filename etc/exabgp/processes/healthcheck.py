#!/usr/bin/env python

"""Healthchecker for exabgp.

This program is to be used as a process for exabgp. It will announce
some VIP depending on the state of a check whose a third-party program
wrapped by this program.

To use, declare this program as a process in your
:file:`/etc/exabgp/exabgp.conf`::

    neighbor 192.0.2.1 {
       router-id 192.0.2.2;
       local-as 64496;
       peer-as 64497;
    }
    process watch-haproxy {
       run /etc/exabgp/processes/healthcheck.py --cmd "curl -sf http://127.0.0.1/healthcheck" --label haproxy;
    }
    process watch-mysql {
       run /etc/exabgp/processes/healthcheck.py --cmd "mysql -u check -e 'SELECT 1'" --label mysql;
    }

Use :option:`--help` to get options accepted by this program. A
configuration file is also possible. Such a configuration file looks
like this::

     debug
     name = haproxy
     interval = 10
     fast-interval = 1
     command = curl -sf http://127.0.0.1/healthcheck

The left-part of each line is the corresponding long option.

When using label for loopback selection, the provided value should
match the beginning of the label without the interface prefix. In the
example above, this means that you should have addresses on lo
labelled ``lo:haproxy1``, ``lo:haproxy2``, etc.

"""

from __future__ import print_function
from __future__ import unicode_literals

import string
import sys
import os
import subprocess
import re
import logging
import logging.handlers
import argparse
import signal
import time
import collections
try:
    # Python 3.3+
    from ipaddress import ip_address
except ImportError:
    # Python 2.6, 2.7, 3.2
    from ipaddr import IPAddress as ip_address
try:
    # Python 3.4+
    from enum import Enum
except ImportError:
    # Other versions. This is not really an enum but this is OK for
    # what we want to do.
    def Enum (*sequential):
        return type(str("Enum"), (), dict(zip(sequential, sequential)))

logger = logging.getLogger("healthcheck")


def parse ():
    """Parse arguments"""
    parser = argparse.ArgumentParser(description=sys.modules[__name__].__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    g = parser.add_mutually_exclusive_group()
    g.add_argument("--debug", "-d", action="store_true",
                   default=False,
                   help="enable debugging")
    g.add_argument("--silent", "-s", action="store_true",
                   default=False,
                   help="don't log to console")
    g.add_argument("--syslog-facility", "-sF", metavar="FACILITY",
                   nargs='?',
                   const="daemon",
                   default="daemon",
                   help="log to syslog using FACILITY, default FACILITY is daemon")
    g.add_argument("--no-syslog", action="store_true",
                   help="disable syslog logging")
    parser.add_argument("--name", "-n", metavar="NAME",
                        help="name for this healthchecker")
    parser.add_argument("--config", "-F", metavar="FILE", type=open,
                        help="read configuration from a file")
    parser.add_argument("--pid", "-p", metavar="FILE", type=argparse.FileType('w'),
                        help="write PID to the provided file")

    g = parser.add_argument_group("checking healthiness")
    g.add_argument("--interval", "-i", metavar='N',
                   default=5,
                   type=float,
                   help="wait N seconds between each healthcheck")
    g.add_argument("--fast-interval", "-f", metavar='N',
                   default=1,
                   type=float, dest="fast",
                   help="when a state change is about to occur, wait N seconds between each healthcheck")
    g.add_argument("--timeout", "-t", metavar='N',
                   default=5,
                   type=int,
                   help="wait N seconds for the check command to execute")
    g.add_argument("--rise", metavar='N',
                   default=3,
                   type=int,
                   help="check N times before considering the service up")
    g.add_argument("--fall", metavar='N',
                   default=3,
                   type=int,
                   help="check N times before considering the service down")
    g.add_argument("--disable", metavar='FILE',
                   type=str,
                   help="if FILE exists, the service is considered disabled")
    g.add_argument("--command", "--cmd", "-c", metavar='CMD',
                   type=str,
                   help="command to use for healthcheck")

    g = parser.add_argument_group("advertising options")
    g.add_argument("--next-hop", "-N", metavar='IP',
                   type=ip_address,
                   help="self IP address to use as next hop")
    g.add_argument("--ip", metavar='IP',
                   type=ip_address, dest="ips", action="append",
                   help="advertise this IP address")
    g.add_argument("--no-ip-setup",
                   action="store_false", dest="ip_setup",
                   help="don't setup missing IP addresses")
    g.add_argument("--label", default=None,
                   help="use the provided label to match loopback addresses")
    g.add_argument("--start-ip", metavar='N',
                   type=int, default=0,
                   help="index of the first IP in the list of IP addresses")
    g.add_argument("--up-metric", metavar='M',
                   type=int, default=100,
                   help="first IP get the metric M when the service is up")
    g.add_argument("--down-metric", metavar='M',
                   type=int, default=1000,
                   help="first IP get the metric M when the service is down")
    g.add_argument("--disabled-metric", metavar='M',
                   type=int, default=500,
                   help="first IP get the metric M when the service is disabled")
    g.add_argument("--increase", metavar='M',
                   type=int, default=1,
                   help="for each additional IP address increase metric value by W")
    g.add_argument("--community", metavar="COMMUNITY",
                   type=str, default=None,
                   help="announce IPs with the supplied community")
    g.add_argument("--withdraw-on-down", action="store_true",
                   help="Instead of increasing the metric on health failure, withdraw the route")

    g = parser.add_argument_group("reporting")
    g.add_argument("--execute", metavar='CMD',
                   type=str, action="append",
                   help="execute CMD on state change")
    g.add_argument("--up-execute", metavar='CMD',
                   type=str, action="append",
                   help="execute CMD when the service becomes available")
    g.add_argument("--down-execute", metavar='CMD',
                   type=str, action="append",
                   help="execute CMD when the service becomes unavailable")
    g.add_argument("--disabled-execute", metavar='CMD',
                   type=str, action="append",
                   help="execute CMD when the service is disabled")

    options = parser.parse_args()
    if options.config is not None:
        # A configuration file has been provided. Read each line and
        # build an equivalent command line.
        args = sum(["--{0}".format(l.strip()).split("=", 1)
                    for l in options.config.readlines()
                    if not l.strip().startswith("#") and l.strip()], [])
        args = [x.strip() for x in args]
        args.extend(sys.argv[1:])
        options = parser.parse_args(args)
    return options


def setup_logging (debug, silent, name, syslog_facility, syslog):
    """Setup logger"""
    logger.setLevel(debug and logging.DEBUG or logging.INFO)
    enable_syslog = syslog and not debug
    # To syslog
    if enable_syslog:
        facility = getattr(logging.handlers.SysLogHandler,
                           "LOG_{0}".format(string.upper(syslog_facility)))
        sh = logging.handlers.SysLogHandler(address=str("/dev/log"),
                                            facility=facility)
        if name:
            healthcheck_name = "healthcheck-{0}".format(name)
        else:
            healthcheck_name = "healthcheck"
        sh.setFormatter(logging.Formatter(
            "{0}[{1}]: %(message)s".format(
                healthcheck_name,
                os.getpid())))
        logger.addHandler(sh)
    # To console
    if sys.stderr.isatty() and not silent:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(levelname)s[%(name)s] %(message)s"))
        logger.addHandler(ch)


def loopback_ips (label):
    """Retrieve loopback IP addresses"""
    logger.debug("Retrieve loopback IP addresses")
    addresses = []

    if sys.platform.startswith("linux"):
        # Use "ip" (ifconfig is not able to see all addresses)
        ipre = re.compile(r"^(?P<index>\d+):\s+(?P<name>\S+)\s+inet6?\s+(?P<ip>[\da-f.:]+)/(?P<netmask>\d+)\s+.*")
        labelre = re.compile(r".*\s+lo:(?P<label>\S+)\s+.*")
        cmd = subprocess.Popen("/sbin/ip -o address show dev lo".split(), shell=False, stdout=subprocess.PIPE)
    else:
        # Try with ifconfig
        ipre = re.compile(r"^\s+inet6?\s+(?P<ip>[\da-f.:]+)\s+(?:netmask 0x(?P<netmask>[0-9a-f]+)|prefixlen (?P<mask>\d+)).*")
        cmd = subprocess.Popen("/sbin/ifconfig lo0".split(), shell=False, stdout=subprocess.PIPE)
        labelre = re.compile(r"")
    for line in cmd.stdout:
        line = line.decode("ascii", "ignore").strip()
        mo = ipre.match(line)
        if not mo:
            continue
        ip = ip_address(mo.group("ip"))
        if not ip.is_loopback:
            if label:
                lmo = labelre.match(line)
                if not lmo or not lmo.group("label").startswith(label):
                    continue
            addresses.append(ip)
    logger.debug("Loopback addresses: {0}".format(addresses))
    return addresses


def setup_ips (ips, label):
    """Setup missing IP on loopback interface"""
    existing = set(loopback_ips(label))
    toadd = set(ips) - existing
    for ip in toadd:
        logger.debug("Setup loopback IP address {0}".format(ip))
        with open(os.devnull, "w") as fnull:
            cmd = ["ip", "address", "add", str(ip), "dev", "lo"]
            if label:
                cmd += ["label", "lo:{0}".format(label)]
            subprocess.check_call(
                cmd, stdout=fnull, stderr=fnull)


def setpgrp_preexec_fn ():
    os.setpgrp()


def check (cmd,timeout):
    """Check the return code of the given command.

    :param cmd: command to execute. If :keyword:`None`, no command is executed.
    :param timeout: how much time we should wait for command completion.
    :return: :keyword:`True` if the command was successful or
             :keyword:`False` if not or if the timeout was triggered.
    """
    if cmd is None:
        return True

    class Alarm(Exception):
        pass

    def alarm_handler (number, frame):  # pylint: disable=W0613
        raise Alarm()

    logger.debug("Checking command {0}".format(repr(cmd)))
    p = subprocess.Popen(cmd, shell=True,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         preexec_fn=setpgrp_preexec_fn)
    if timeout:
        signal.signal(signal.SIGALRM, alarm_handler)
        signal.alarm(timeout)
    try:
        stdout = None
        stdout, _ = p.communicate()
        if timeout:
            signal.alarm(0)
        if p.returncode != 0:
            logger.warn("Check command was unsuccessful: {0}".format(p.returncode))
            if stdout.strip():
                logger.info("Output of check command: {0}".format(stdout))
            return False
        logger.debug("Command was executed successfully {0} {1}".format(p.returncode, stdout))
        return True
    except Alarm:
        logger.warn("Timeout ({0}) while running check command {1}".format(timeout, cmd))
        os.killpg(p.pid, signal.SIGKILL)
        return False


def loop (options):
    """Main loop."""
    states = Enum(
        "INIT",                 # Initial state
        "DISABLED",             # Disabled state
        "RISING",               # Checks are currently succeeding.
        "FALLING",              # Checks are currently failing.
        "UP",                   # Service is considered as up.
        "DOWN",                 # Service is considered as down.
    )
    state = states.INIT

    def exabgp (target):
        """Communicate new state to ExaBGP"""
        if target not in (states.UP, states.DOWN, states.DISABLED):
            return
        logger.info("send announces for {0} state to ExaBGP".format(target))
        metric = vars(options).get("{0}_metric".format(str(target).lower()))
        for ip in options.ips:
            if options.withdraw_on_down:
                command = "announce" if target is states.UP else "withdraw"
            else:
                command = "announce"
            announce = "route {0}/{1} next-hop {2}".format(str(ip),
                                                           ip.max_prefixlen,
                                                           options.next_hop or "self")
            if command == "announce":
                announce = "{0} med {1}".format(announce, metric)
                if options.community:
                    announce = "{0} community [ {1} ]".format(announce,
                                                              options.community)
            logger.debug("exabgp: {0} {1}".format(command, announce))
            print("{0} {1}".format(command, announce))
            metric += options.increase
        sys.stdout.flush()

    def trigger (target):
        """Trigger a state change and execute the appropriate commands"""
        # Shortcut for RISING->UP and FALLING->UP
        if target == states.RISING and options.rise <= 1:
            target = states.UP
        elif target == states.FALLING and options.fall <= 1:
            target = states.DOWN

        # Log and execute commands
        logger.debug("Transition to {0}".format(str(target)))
        cmds = []
        cmds.extend(vars(options).get("{0}_execute".format(str(target).lower()), []) or [])
        cmds.extend(vars(options).get("execute", []) or [])
        for cmd in cmds:
            logger.debug("Transition to {0}, execute `{1}`".format(str(target), cmd))
            env = os.environ.copy()
            env.update({"STATE": str(target)})
            with open(os.devnull, "w") as fnull:
                subprocess.call(
                    cmd, shell=True, stdout=fnull, stderr=fnull, env=env)

        return target

    checks = 0
    while True:
        disabled = options.disable is not None and os.path.exists(options.disable)
        successful = disabled or check(options.command, options.timeout)
        # FSM
        if state != states.DISABLED and disabled:
            state = trigger(states.DISABLED)
        elif state == states.INIT:
            if successful and options.rise <= 1:
                state = trigger(states.UP)
            elif successful:
                state = trigger(states.RISING)
                checks = 1
            else:
                state = trigger(states.FALLING)
                checks = 1
        elif state == states.DISABLED:
            if not disabled:
                state = trigger(states.INIT)
        elif state == states.RISING:
            if successful:
                checks += 1
                if checks >= options.rise:
                    state = trigger(states.UP)
            else:
                state = trigger(states.FALLING)
                checks = 1
        elif state == states.FALLING:
            if not successful:
                checks += 1
                if checks >= options.fall:
                    state = trigger(states.DOWN)
            else:
                state = trigger(states.RISING)
                checks = 1
        elif state == states.UP:
            if not successful:
                state = trigger(states.FALLING)
                checks = 1
        elif state == states.DOWN:
            if successful:
                state = trigger(states.RISING)
                checks = 1
        else:
            raise ValueError("Unhandled state: {0}".format(str(state)))

        # Send announces. We announce them on a regular basis in case
        # we lose connection with a peer.
        exabgp(state)

        # How much we should sleep?
        if state in (states.FALLING, states.RISING):
            time.sleep(options.fast)
        else:
            time.sleep(options.interval)

if __name__ == "__main__":
    options = parse()
    setup_logging(options.debug, options.silent, options.name,
                  options.syslog_facility, not options.no_syslog)
    if options.pid:
        options.pid.write("{0}\n".format(os.getpid()))
        options.pid.close()
    try:
        # Setup IP to use
        options.ips = options.ips or loopback_ips(options.label)
        if not options.ips:
            raise RuntimeError("No IP found")
        if options.ip_setup:
            setup_ips(options.ips, options.label)
        options.ips = collections.deque(options.ips)
        options.ips.rotate(-options.start_ip)
        options.ips = list(options.ips)
        # Main loop
        loop(options)
    except Exception as e:
        logger.exception("Uncatched exception: %s", e)
