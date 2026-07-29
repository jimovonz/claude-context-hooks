#!/usr/bin/env python3
"""
ssh-tool — persistent, named SSH sessions for Claude, cch-cached for free.

Why this exists: every plain `ssh` invocation from a Bash tool call pays a
fresh handshake+auth cost, and there was no shared way to keep a session
alive across tool calls, tunnel to a device's services, or kick off a
long-running remote job without blocking a turn. This wraps OpenSSH's
ControlMaster/ControlPersist multiplexing behind named sessions.

Deliberately does NOT implement any output caching itself: the existing
Bash PreToolUse hook chain (rtk hook claude -> intercept-bash.py ->
cache-wrap.py) auto-wraps any command that isn't a passthrough marker, so
`ssh-tool.py run ...` invoked as a normal Bash command already gets RTK
compression + >8KB CCM caching + fail-soft exit handling for free -- and so
does piping multiple `ssh-tool.py run <name> -- cmd` lines (one per host)
into the existing cch-batch.py for real concurrent fan-out. Adding any
caching logic here would double-process.

Plain `ssh`/`sshpass` remain fully available for anything this tool
doesn't cover -- this is additive, not a replacement.

Usage:
  ssh-tool.py open ugv3-nav root@10.147.20.31 --password-file ~/.claude/secrets/ugv3 --no-host-key-check
  ssh-tool.py open basestation1 ubuntu@basestation.local -A -i ~/.ssh/id_field
  ssh-tool.py run ugv3-nav -- systemctl status nav-controller
  ssh-tool.py run ugv3-nav --detach -- ./long_calibration.sh
  ssh-tool.py jobs ugv3-nav
  ssh-tool.py tail ugv3-nav <label>
  ssh-tool.py tunnel basestation1 -L 8080:localhost:80
  ssh-tool.py copy ugv3-nav :/var/log/nav.log ./nav.log
  ssh-tool.py list
  ssh-tool.py reset ugv3-nav --password-file ~/.claude/secrets/ugv3
  ssh-tool.py close ugv3-nav
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SSH_DIR = Path.home() / '.claude' / 'cache' / 'ssh'
SOCKETS_DIR = SSH_DIR / 'sockets'
JOBS_DIR_REMOTE = '~/.ssh-tool-jobs'
REGISTRY_PATH = SSH_DIR / 'sessions.json'
LOCK_PATH = SSH_DIR / 'sessions.json.lock'
CONNECT_TIMEOUT = '10'


def _ensure_dirs():
    SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SOCKETS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SSH_DIR, 0o700)
    os.chmod(SOCKETS_DIR, 0o700)


@contextmanager
def session_registry(exclusive=True):
    """Locked, atomic read-modify-write over the session registry.

    Yields the parsed dict ({"sessions": {...}}); on exit (if exclusive),
    writes it back atomically via tmp-file + os.replace. No atomic-write
    helper exists in lib/ccm_cache.py to reuse (it relies on content
    addressing, not applicable to a mutable registry), so this rolls its
    own flock + os.replace.
    """
    _ensure_dirs()
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            data = json.loads(REGISTRY_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {'sessions': {}}
        yield data
        if exclusive:
            tmp = REGISTRY_PATH.with_suffix(f'.json.tmp.{os.getpid()}')
            tmp.write_text(json.dumps(data, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, REGISTRY_PATH)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def socket_path(name: str) -> str:
    h = hashlib.blake2s(name.encode(), digest_size=6).hexdigest()
    return str(SOCKETS_DIR / h)


def die(msg: str, code: int = 1):
    print(f'ssh-tool: {msg}', file=sys.stderr)
    sys.exit(code)


def get_session(reg: dict, name: str) -> dict:
    sess = reg['sessions'].get(name)
    if sess is None:
        open_names = ', '.join(sorted(reg['sessions'])) or '(none)'
        die(
            f"session '{name}' not found. Open sessions: [{open_names}].\n"
            f"Open it first:  ssh-tool.py open {name} user@host [--password-file PATH]"
        )
    return sess


def user_at_host(sess: dict) -> str:
    return f"{sess['user']}@{sess['host']}"


def base_ssh_opts(sess: dict, batch_mode: bool = True) -> list:
    opts = ['-o', f'ConnectTimeout={CONNECT_TIMEOUT}']
    if batch_mode:
        opts += ['-o', 'BatchMode=yes']
    if sess.get('port'):
        opts += ['-p', str(sess['port'])]
    if sess.get('no_host_key_check'):
        opts += ['-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null']
    else:
        opts += ['-o', 'StrictHostKeyChecking=accept-new']
    if sess.get('identity_file'):
        opts += ['-i', sess['identity_file']]
    if sess.get('jump'):
        opts += ['-J', sess['jump']]
    for o in sess.get('extra_opts', []):
        opts += ['-o', o]
    return opts


def is_alive(sess: dict) -> bool:
    r = subprocess.run(
        ['ssh', '-O', 'check', '-S', sess['control_path'], user_at_host(sess)],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def read_password(args) -> str | None:
    if args.password_file:
        p = Path(args.password_file).expanduser()
        if not p.exists():
            die(f'password file not found: {p}')
        return p.read_text().splitlines()[0] if p.read_text() else ''
    if args.password_env:
        val = os.environ.get(args.password_env)
        if val is None:
            die(f'env var {args.password_env!r} is not set')
        return val
    return None


def _split_forward(spec: str):
    # spec like "8080:localhost:80" or "0.0.0.0:8080:localhost:80"
    return spec


CHANGED_HOSTKEY_RE = re.compile(
    r"ssh-keygen -f '([^']+)' -R '([^']+)'"
)


def _remediate_changed_hostkey(stderr: str) -> bool:
    """If stderr shows OpenSSH's 'REMOTE HOST IDENTIFICATION HAS CHANGED'
    warning, run the exact ssh-keygen -R removal it suggests and report True.

    This fleet's devices get reflashed/redeployed routinely (confirmed by
    the user hitting and manually clearing this exact warning against
    ugv-rpltko-basestation), and the ZeroTier bastion already gates network
    access — so auto-clearing a stale known_hosts entry on a detected KEY
    CHANge (never on first contact, which stays subject to accept-new) is
    the correct default here, not a hole to guard against. Always logged,
    never silent.
    """
    m = CHANGED_HOSTKEY_RE.search(stderr)
    if not m:
        return False
    known_hosts_path, host = m.group(1), m.group(2)
    subprocess.run(['ssh-keygen', '-f', known_hosts_path, '-R', host],
                    capture_output=True)
    print(f"[ssh-tool: host key for {host} changed; cleared stale "
          f"known_hosts entry and retrying]", file=sys.stderr)
    return True


def do_open(args):
    name = args.name
    with session_registry() as reg:
        if name in reg['sessions']:
            if not args.replace:
                die(
                    f"session '{name}' already exists. Use --replace to tear "
                    f"down and reopen, or pick a different name."
                )
            old = reg['sessions'].pop(name)
            subprocess.run(
                ['ssh', '-O', 'exit', '-S', old['control_path'], f"{old['user']}@{old['host']}"],
                capture_output=True,
            )

        if '@' not in args.target:
            die("target must be user@host, e.g. root@10.147.20.31")
        user, host = args.target.split('@', 1)

        sock = socket_path(name)
        password = read_password(args)

        sess = {
            'name': name, 'user': user, 'host': host,
            'port': args.port, 'control_path': sock,
            'forward_agent': args.forward_agent,
            'identity_file': str(Path(args.identity).expanduser().resolve()) if args.identity else None,
            'jump': args.jump, 'persist': args.persist,
            'needs_password': password is not None,
            'no_host_key_check': args.no_host_key_check,
            'extra_opts': args.opt or [],
            'tunnels': [{'direction': d, 'spec': s} for d, s in (args.forward or [])],
            'opened_at': time.time(), 'last_used_at': time.time(),
        }

        cmd = ['ssh', '-M', '-N', '-f', '-S', sock]
        cmd += ['-o', 'ControlMaster=yes', '-o', f'ControlPersist={args.persist}']
        cmd += base_ssh_opts(sess, batch_mode=password is None)
        if password is not None:
            cmd += ['-o', 'NumberOfPasswordPrompts=1']
        if sess['forward_agent']:
            cmd += ['-A']
        for direction, spec in (args.forward or []):
            cmd += [direction, spec]
        cmd.append(user_at_host(sess))

        env = os.environ.copy()
        if password is not None:
            env['SSHPASS'] = password
            cmd = ['sshpass', '-e'] + cmd

        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0 and _remediate_changed_hostkey(r.stderr):
            r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            stderr = r.stderr.strip()
            if 'Permission denied' in stderr or 'password' in stderr.lower():
                die(f"auth failed opening '{name}' ({user_at_host(sess)}): {stderr}\n"
                    f"Check the password/identity, or try --no-host-key-check if host keys rotated.")
            die(f"failed to open '{name}' ({user_at_host(sess)}): {stderr or '(no stderr)'}")

        if not is_alive(sess):
            die(f"master for '{name}' did not come up (no live control socket after connect).")

        reg['sessions'][name] = sess
    print(f"opened '{name}' -> {user_at_host(sess)} (persist={args.persist})")


def _reconnect(name: str, sess: dict, password: str | None = None):
    """Re-run the open sequence from stored params. Returns True on success."""
    cmd = ['ssh', '-M', '-N', '-f', '-S', sess['control_path']]
    cmd += ['-o', 'ControlMaster=yes', '-o', f"ControlPersist={sess['persist']}"]
    cmd += base_ssh_opts(sess, batch_mode=password is None)
    if password is not None:
        cmd += ['-o', 'NumberOfPasswordPrompts=1']
    if sess.get('forward_agent'):
        cmd += ['-A']
    for t in sess.get('tunnels', []):
        cmd += [t['direction'], t['spec']]
    cmd.append(user_at_host(sess))

    env = os.environ.copy()
    if password is not None:
        env['SSHPASS'] = password
        cmd = ['sshpass', '-e'] + cmd

    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0 and _remediate_changed_hostkey(r.stderr):
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return r.returncode == 0 and is_alive(sess)


def _ensure_live(name: str, sess: dict, reg: dict, allow_password: str | None = None) -> None:
    if is_alive(sess):
        return
    if sess.get('needs_password'):
        die(
            f"session '{name}' master is dead. Password sessions can't "
            f"auto-reconnect (the password is never stored). Re-run:\n"
            f"  ssh-tool.py reset {name} --password-file PATH"
        )
    print(f"[ssh-tool: master '{name}' was dead; reconnected]", file=sys.stderr)
    if not _reconnect(name, sess):
        die(f"failed to auto-reconnect '{name}'.")


def do_run(args):
    with session_registry() as reg:
        sess = get_session(reg, args.name)
        _ensure_live(args.name, sess, reg)
        sess['last_used_at'] = time.time()

    remote_cmd = ' '.join(args.command)
    if not remote_cmd:
        die('no remote command given (usage: ssh-tool.py run <name> -- <command>)')

    # Plain non-interactive ssh commands never source ~/.bashrc (Ubuntu's
    # default .bashrc explicitly early-exits via `case $- in *i*) ;; *) return;;
    # esac` when it's not an interactive shell), so aliases/functions/PATH
    # additions defined there (a common pattern on this fleet, e.g. the
    # `leases` helper living in ~/.local/bin) silently don't exist for a bare
    # `ssh host -- cmd`. Running the command through `bash -lic` forces bash to
    # treat itself as an interactive login shell so both .bashrc AND the
    # login-only .profile (where ~/.local/bin gets added to PATH on this
    # fleet) actually get sourced, at the
    # cost of a benign "cannot set terminal process group" stderr warning
    # (no controlling tty over a plain ssh channel) -- expected noise, not a
    # real error; the command's real output/exit code are unaffected.
    if not args.no_bashrc:
        remote_cmd = f'bash -lic {shlex.quote(remote_cmd)}'

    if args.detach:
        label = args.label or f'job-{int(time.time())}-{os.getpid()}'
        job_dir = f'{JOBS_DIR_REMOTE}/{label}'
        # Backgrounding a multi-command "&&" chain directly (mkdir && nohup ... &)
        # leaves a duplicate of the SSH channel's fd open in the subshell bash
        # keeps around to restore state after the per-command redirect, which
        # blocks the local `ssh` call until the job finishes (observed: hangs
        # for the job's full duration). Backgrounding a SINGLE simple command
        # (nohup sh -c '<everything>' &) avoids that fd-save/restore entirely,
        # so the whole compound goes inside the nested sh -c's script instead.
        inner_script = (
            f"mkdir -p {job_dir} && "
            f"{{ {remote_cmd}; echo $? > {job_dir}/exit_code; }} "
            f">{job_dir}/out.log 2>&1"
        )
        wrapped = (
            f"nohup sh -c {shlex.quote(inner_script)} "
            f"</dev/null >/dev/null 2>&1 &"
        )
        cmd = ['ssh', '-S', sess['control_path'], '-o', 'ControlMaster=no',
               '-o', 'BatchMode=yes']
        if sess.get('forward_agent'):
            cmd += ['-A']
        cmd += [user_at_host(sess), '--', wrapped]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            sys.exit(r.returncode)
        print(f"detached: label={label} log={job_dir}/out.log "
              f"(check with: ssh-tool.py tail {args.name} {label})")
        return

    # Agent forwarding is negotiated per session channel, not inherited from
    # however the master was originally opened -- each multiplexed session
    # that needs the forwarded agent (e.g. a remote git clone over ssh) must
    # request it again here, or SSH_AUTH_SOCK is empty in that session even
    # though the master itself was opened with -A.
    cmd = ['ssh', '-S', sess['control_path'], '-o', 'ControlMaster=no',
           '-o', 'BatchMode=yes']
    if sess.get('forward_agent'):
        cmd += ['-A']
    cmd += [user_at_host(sess), '--', remote_cmd]
    r = subprocess.run(cmd)
    sys.exit(r.returncode)


def do_jobs(args):
    with session_registry(exclusive=False) as reg:
        sess = get_session(reg, args.name)
        _ensure_live(args.name, sess, reg)

    list_cmd = (
        f"for d in {JOBS_DIR_REMOTE}/*/; do "
        f"[ -d \"$d\" ] || continue; "
        f"label=$(basename \"$d\"); "
        f"if [ -f \"$d/exit_code\" ]; then "
        f"echo \"$label DONE exit=$(cat \"$d/exit_code\")\"; "
        f"else echo \"$label RUNNING\"; fi; "
        f"done"
    )
    cmd = ['ssh', '-S', sess['control_path'], '-o', 'ControlMaster=no',
           '-o', 'BatchMode=yes', user_at_host(sess), '--', list_cmd]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stdout.strip()
    if not out:
        print('(no jobs)')
    else:
        print(out)

    if args.clean:
        clean_cmd = (
            f"for d in {JOBS_DIR_REMOTE}/*/; do "
            f"[ -f \"$d/exit_code\" ] && rm -rf \"$d\"; done"
        )
        subprocess.run(['ssh', '-S', sess['control_path'], '-o', 'ControlMaster=no',
                         user_at_host(sess), '--', clean_cmd])
        print('cleaned finished jobs')


def do_tail(args):
    with session_registry() as reg:
        sess = get_session(reg, args.name)
        _ensure_live(args.name, sess, reg)
        sess['last_used_at'] = time.time()

    job_dir = f'{JOBS_DIR_REMOTE}/{args.label}'
    tail_cmd = f"tail -n {args.lines} {job_dir}/out.log; " \
               f"[ -f {job_dir}/exit_code ] && echo \"[exit $(cat {job_dir}/exit_code)]\""
    if args.follow:
        tail_cmd = f"tail -n {args.lines} -f {job_dir}/out.log"
    cmd = ['ssh', '-S', sess['control_path'], '-o', 'ControlMaster=no',
           user_at_host(sess), '--', tail_cmd]
    r = subprocess.run(cmd)
    sys.exit(r.returncode)


def do_tunnel(args, cancel=False):
    with session_registry() as reg:
        sess = get_session(reg, args.name)
        _ensure_live(args.name, sess, reg)

        direction = '-L' if args.local else '-R'
        action = 'cancel' if cancel else 'forward'
        cmd = ['ssh', '-S', sess['control_path'], '-O', action,
               direction, args.spec, user_at_host(sess)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            die(f"{'untunnel' if cancel else 'tunnel'} failed: {r.stderr.strip()}")

        tunnels = sess.setdefault('tunnels', [])
        if cancel:
            sess['tunnels'] = [t for t in tunnels
                                if not (t['direction'] == direction and t['spec'] == args.spec)]
        else:
            tunnels.append({'direction': direction, 'spec': args.spec})
    verb = 'removed' if cancel else 'added'
    print(f"{verb} {direction} {args.spec} on '{args.name}'")


def do_copy(args):
    with session_registry() as reg:
        sess = get_session(reg, args.name)
        _ensure_live(args.name, sess, reg)
        sess['last_used_at'] = time.time()

    def resolve(p):
        if p.startswith(':'):
            return f"{user_at_host(sess)}:{p[1:]}"
        return p

    cmd = ['scp', '-o', f'ControlPath={sess["control_path"]}', '-o', 'ControlMaster=no']
    if sess.get('port'):
        cmd += ['-P', str(sess['port'])]
    cmd += [resolve(args.src), resolve(args.dst)]
    r = subprocess.run(cmd)
    sys.exit(r.returncode)


def do_reset(args):
    with session_registry() as reg:
        sess = get_session(reg, args.name)
        subprocess.run(['ssh', '-O', 'exit', '-S', sess['control_path'], user_at_host(sess)],
                        capture_output=True)
        password = read_password(args) if (args.password_file or args.password_env) else None
        if sess.get('needs_password') and password is None:
            die(f"session '{args.name}' needs a password to reconnect. Re-run with "
                f"--password-file PATH or --password-env VAR.")
        if not _reconnect(args.name, sess, password=password):
            die(f"failed to reconnect '{args.name}'.")
        sess['last_used_at'] = time.time()
    print(f"reset '{args.name}' -> {user_at_host(sess)}")


def do_close(args):
    with session_registry() as reg:
        if args.all:
            names = list(reg['sessions'])
        else:
            get_session(reg, args.name)
            names = [args.name]
        for name in names:
            sess = reg['sessions'].pop(name)
            subprocess.run(['ssh', '-O', 'exit', '-S', sess['control_path'], user_at_host(sess)],
                            capture_output=True)
            try:
                os.unlink(sess['control_path'])
            except FileNotFoundError:
                pass
            print(f"closed '{name}'")


def do_list(args):
    with session_registry(exclusive=False) as reg:
        sessions = reg['sessions']
        if args.json:
            out = []
            for name, sess in sessions.items():
                out.append({**sess, 'alive': is_alive(sess)})
            print(json.dumps(out, indent=2))
            return
        if not sessions:
            print('(no open sessions)')
            return
        for name, sess in sessions.items():
            alive = is_alive(sess)
            if alive:
                status = 'ALIVE'
            elif sess.get('needs_password'):
                status = 'DEAD (needs reset)'
            else:
                status = 'DEAD (will auto-reconnect on next run)'
            tunnels = ', '.join(f"{t['direction']} {t['spec']}" for t in sess.get('tunnels', [])) or '-'
            last_used = time.strftime('%H:%M:%S', time.localtime(sess.get('last_used_at', 0)))
            print(f"{name:20} {status:28} {user_at_host(sess):30} "
                  f"agent={'yes' if sess.get('forward_agent') else 'no':<3} "
                  f"persist={sess.get('persist'):6} tunnels=[{tunnels}] last_used={last_used}")


def parse_forward(value):
    # value like "-L8080:localhost:80" won't reach here; argparse gives us
    # the spec via a dedicated --local/--remote-spec pair instead for `tunnel`,
    # but `open` needs repeatable -L/-R with attached specs.
    return value


def build_parser():
    p = argparse.ArgumentParser(prog='ssh-tool.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    op = sub.add_parser('open', help='establish a persistent SSH master')
    op.add_argument('name')
    op.add_argument('target', help='user@host')
    op.add_argument('-A', '--forward-agent', action='store_true')
    op.add_argument('-p', '--port', type=int)
    op.add_argument('-i', '--identity')
    op.add_argument('-J', '--jump')
    op.add_argument('--persist', default='30m')
    op.add_argument('--password-file')
    op.add_argument('--password-env', nargs='?', const='SSHPASS', default=None)
    op.add_argument('--no-host-key-check', action='store_true')
    op.add_argument('-o', '--opt', action='append')
    op.add_argument('-L', dest='forward', action='append', nargs=1,
                     metavar='SPEC', help='local port forward, e.g. -L 8080:localhost:80')
    op.add_argument('-R', dest='forward_r', action='append', nargs=1, metavar='SPEC')
    op.add_argument('--replace', action='store_true')
    op.set_defaults(func=do_open)

    run = sub.add_parser('run', help='run a command on an open session')
    run.add_argument('name')
    run.add_argument('--detach', action='store_true')
    run.add_argument('--label')
    run.add_argument('--no-bashrc', action='store_true',
                      help="don't source ~/.bashrc (skip the bash -ic wrap)")
    run.add_argument('command', nargs='*')
    run.set_defaults(func=do_run)

    jobs = sub.add_parser('jobs', help='list detached jobs on a session')
    jobs.add_argument('name')
    jobs.add_argument('--clean', action='store_true')
    jobs.set_defaults(func=do_jobs)

    tail = sub.add_parser('tail', help='tail a detached job\'s output')
    tail.add_argument('name')
    tail.add_argument('label')
    tail.add_argument('-n', '--lines', type=int, default=50)
    tail.add_argument('--follow', action='store_true')
    tail.set_defaults(func=do_tail)

    tun = sub.add_parser('tunnel', help='add a port forward to a live session')
    tun.add_argument('name')
    g = tun.add_mutually_exclusive_group(required=True)
    g.add_argument('-L', dest='local', action='store_true')
    g.add_argument('-R', dest='local', action='store_false')
    tun.add_argument('spec', help='e.g. 8080:localhost:80')
    tun.set_defaults(func=lambda a: do_tunnel(a, cancel=False))

    untun = sub.add_parser('untunnel', help='remove a port forward from a live session')
    untun.add_argument('name')
    g2 = untun.add_mutually_exclusive_group(required=True)
    g2.add_argument('-L', dest='local', action='store_true')
    g2.add_argument('-R', dest='local', action='store_false')
    untun.add_argument('spec')
    untun.set_defaults(func=lambda a: do_tunnel(a, cancel=True))

    copy = sub.add_parser('copy', help='scp over an open session (":" prefix = remote side)')
    copy.add_argument('name')
    copy.add_argument('src')
    copy.add_argument('dst')
    copy.set_defaults(func=do_copy)

    reset = sub.add_parser('reset', help='recycle a hung/stale master')
    reset.add_argument('name')
    reset.add_argument('--password-file')
    reset.add_argument('--password-env', nargs='?', const='SSHPASS', default=None)
    reset.set_defaults(func=do_reset)

    close = sub.add_parser('close', help='tear down a session, no reconnect')
    close.add_argument('name', nargs='?')
    close.add_argument('--all', action='store_true')
    close.set_defaults(func=do_close)

    lst = sub.add_parser('list', help='show open sessions')
    lst.add_argument('--json', action='store_true')
    lst.set_defaults(func=do_list)

    return p


def main():
    # Split argv on the first literal '--' before argparse sees it, so the
    # remote command (which may itself contain flags like -n, -la, etc.)
    # is never misparsed by our own argparse. Mirrors cache-wrap.py's own
    # '--' convention.
    argv = sys.argv[1:]
    head, remote_cmd_tail = argv, []
    if '--' in argv:
        idx = argv.index('--')
        head, remote_cmd_tail = argv[:idx], argv[idx + 1:]

    parser = build_parser()
    args = parser.parse_args(head)
    if remote_cmd_tail:
        args.command = remote_cmd_tail
    elif getattr(args, 'cmd', None) == 'run' and not getattr(args, 'command', None):
        die('usage: ssh-tool.py run <name> -- <command>')

    if args.cmd == 'open':
        forwards = [('-L', s[0]) for s in (args.forward or [])]
        forwards += [('-R', s[0]) for s in (args.forward_r or [])]
        args.forward = forwards

    args.func(args)


if __name__ == '__main__':
    main()
