#!/usr/bin/env python3
"""Build and run the upstream Contiki-NG tests selected in instance.json."""

import json
import os
import platform
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path


TESTBED = Path('/testbed')
CONFIG = Path('/opt/embedeval/instance.json')
LOGS = TESTBED / '.embedeval' / 'logs'
BUILD_TIMEOUT = 900
RUN_TIMEOUT = 300

UNIT_LINE = re.compile(r'^Unit test: (.+?)\s*$')
RESULT_LINE = re.compile(r'^Result: (success|failure)\s*$')
LEGACY_LINE = re.compile(r'^=check-me= (SUCCEEDED|FAILED)\s+- (.+?)\s*$')
DONE_LINE = re.compile(r'^=check-me= DONE\s*$')
COOJA_LINE = re.compile(r'\bTEST (OK|FAIL(?:ED)?)\b')   # Ant-era simexec writes TEST FAIL
SCRIPT_SUCCESS = re.compile(r'^=check-me= SUCCEEDED - (.+?)\s*$')
SCRIPT_FAILURE = re.compile(r'^=check-me= FAILED - (.+?)\s*$')
CRASH_FRAME = re.compile(r'^#\d+\s+(?:0x[0-9a-fA-F]+ in )?unit_test_function_(\w+) \(', re.M)
CRASH_TIMEOUT = 120


def parse_native(text):
    """Return description statuses and whether the native event loop completed."""
    cases = {}
    pending = None
    done = False
    for line in text.splitlines():
        line = line.strip()
        match = UNIT_LINE.match(line)
        if match:
            pending = match.group(1)
            continue
        match = RESULT_LINE.match(line)
        if match and pending is not None:
            cases[pending] = 'passed' if match.group(1) == 'success' else 'failed'
            pending = None
            continue
        match = LEGACY_LINE.match(line)
        if match:
            description = match.group(2)
            if match.group(1) == 'FAILED':
                description = re.sub(r'(?:: exit at L\d+|\s+\(exit at \d+\))$', '', description)
            cases[description] = 'passed' if match.group(1) == 'SUCCEEDED' else 'failed'
            pending = None
            continue
        if DONE_LINE.match(line):
            done = True
            break
    return cases, done


def parse_cooja(text):
    """Require an explicit simulator verdict in a fresh COOJA.testlog."""
    verdicts = COOJA_LINE.findall(text)
    if 'FAILED' in verdicts or 'FAIL' in verdicts:
        return 'failed'
    return 'passed' if 'OK' in verdicts else 'not_run'


def cooja_build_failed(rc, verdict, output):
    return (rc != 0 and verdict == 'not_run'
            and re.search(r'Compilation failed|Compilation process returned error code|'
                          r'Execution failed for task [\'\"]:compile|\bcompileJava FAILED\b',
                          output, re.IGNORECASE) is not None)


# Cooja could not build a mote or load the simulation, so no scenario ran.
# Ant-era simexec reports this as TEST FAIL; it is never a runtime failure.
COOJA_LOAD_FAILURE = re.compile(r'Mote type creation (?:error|failed)|Exception when loading simulation')


def parse_script(text, entry, summary=''):
    """Require the selected upstream entry and its successful suite summary."""
    fields = entry.split(':')
    selected_path = fields[0].rstrip('/').split('/')[-1] + '/'
    successes = []
    for line in text.splitlines():
        match = SCRIPT_SUCCESS.match(line.strip())
        if match:
            successes.append(match.group(1))
        match = SCRIPT_FAILURE.match(line.strip())
        if match:
            return 'failed'
    if re.search(r'\bTEST FAIL(?:ED)?\b', summary) or re.search(r'\bTEST FAILED\b', text):
        return 'failed'
    if entry in successes:
        return 'passed'
    named_build = any(
        line.startswith('Building:') and selected_path in line
        for line in text.splitlines()
    )
    return 'passed' if named_build and 'All tests OK' in text else 'not_run'


def log_path(unit, phase):
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', unit['id'])
    return LOGS / '{}.{}.log'.format(safe, phase)


def run_logged(cmd, path, cwd=TESTBED, env=None, timeout=BUILD_TIMEOUT):
    print('$ ' + ' '.join(cmd), flush=True)
    try:
        # Bytes, decoded here: the oldest CI images run Python 3.5.
        result = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        output = (result.stdout + result.stderr).decode(errors='replace')
        rc = result.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b''
        if isinstance(output, bytes):
            output = output.decode(errors='replace')
        error = exc.stderr or b''
        if isinstance(error, bytes):
            error = error.decode(errors='replace')
        output += error
        rc = 'timeout'
    path.write_text(output)
    if rc != 0:
        print('\n'.join(output.splitlines()[-30:]), flush=True)
    return rc, output


def native_binary(unit):
    directory = TESTBED / unit['directory']
    binary = unit['binary']
    for candidate in (directory / 'build/native' / (binary + '.native'),
                      directory / (binary + '.native')):
        if candidate.is_file():
            return candidate
    return None


def run_native_binary(binary, path, cwd=None):
    """Read output until DONE, process exit, or timeout; then stop the event loop."""
    cmd = ['stdbuf', '-oL', '-eL', str(binary)]
    print('$ ' + ' '.join(cmd), flush=True)
    process = subprocess.Popen(cmd, cwd=str(cwd or binary.parent), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    chunks = []
    line_buffer = b''
    done = False
    timed_out = False
    deadline = time.monotonic() + RUN_TIMEOUT
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(min(remaining, 1))
            if not events:
                if process.poll() is not None:
                    break
                continue
            data = os.read(process.stdout.fileno(), 65536)
            if not data:
                break
            chunks.append(data)
            line_buffer += data
            while b'\n' in line_buffer:
                line, line_buffer = line_buffer.split(b'\n', 1)
                if DONE_LINE.match(line.decode(errors='replace').strip()):
                    done = True
                    break
            if done:
                break
    finally:
        selector.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    output = b''.join(chunks).decode(errors='replace')
    path.write_text(output)
    return output, done, timed_out, process.returncode


def build_native(unit):
    stamp = LOGS / ("config-" + re.sub(r'[^A-Za-z0-9_.-]', '_', unit['directory']) + '.json')
    options = json.dumps(unit.get('make_options', []), sort_keys=True)
    if stamp.exists() and stamp.read_text() != options:
        rc, _ = run_logged(['make', '-C', str(TESTBED / unit['directory']), 'TARGET=native', 'clean'], log_path(unit, 'clean'))
        if rc != 0:
            return False
    stamp.write_text(options)
    cmd = ['make', '-C', str(TESTBED / unit['directory']), 'TARGET=native',
           'WERROR=1', *unit.get('make_options', []), unit['binary'] + '.native']
    rc, _ = run_logged(cmd, log_path(unit, 'build'))
    return rc == 0 and native_binary(unit) is not None


def crashed_case(text):
    """The registered case of the innermost unit_test_function_<name> frame
    after a signal in a gdb backtrace, or None."""
    _, signalled, trace = text.partition('received signal')
    if not signalled:
        return None
    match = CRASH_FRAME.search(trace)
    return match.group(1) if match else None


def attribute_crash(unit, binary):
    """Rerun a binary that a signal killed under gdb, once. The framework
    reports a case only after its body returns, so a crash leaves no name in
    the output; the backtrace names the case whose body was running."""
    cmd = ['timeout', '-k', '5', str(CRASH_TIMEOUT), 'gdb', '-q', '-batch', '-nx',
           '-ex', 'run', '-ex', 'bt', '--args', str(binary)]
    _, output = run_logged(cmd, log_path(unit, 'crash'), cwd=TESTBED / unit['directory'],
                           timeout=CRASH_TIMEOUT + 30)
    return crashed_case(output)


def run_native(unit):
    expected = {case['description']: unit['id'] + '::' + case['id']
                for case in unit.get('cases', [])}
    binary = native_binary(unit)
    output, stopped, timed_out, rc = run_native_binary(binary,
                                                       log_path(unit, 'run'), TESTBED / unit['directory'])
    observed, parsed_done = parse_native(output)
    complete = stopped and parsed_done
    if not complete:
        print('{}: native completion marker missing (exit={}, timeout={})'.format(unit['id'], rc, timed_out))
    statuses = {identifier: observed.get(description, 'not_run')
                for description, identifier in expected.items()}
    if not complete and not timed_out and rc is not None and rc < 0:
        name = attribute_crash(unit, binary)
        identifier = unit['id'] + '::' + name if name else None
        if identifier in statuses and statuses[identifier] == 'not_run':
            statuses[identifier] = 'error'
            print('{}: signal {} inside case {} (gdb backtrace); later cases did not run'
                  .format(unit['id'], -rc, name))
        else:
            print('{}: signal {} not attributable to a registered case'.format(unit['id'], -rc))
    return statuses, complete


def cooja_java_home():
    """A JDK whose include/jni.h Cooja's native motes compile against: the
    image's own JAVA_HOME, else /usr/local (current images), else the JDK that
    javac resolves to (2022 images set no JAVA_HOME at all)."""
    import shutil
    candidates = [os.environ.get('JAVA_HOME'), '/usr/local']
    javac = shutil.which('javac')
    if javac:
        candidates.append(str(Path(os.path.realpath(javac)).parent.parent))
    for home in candidates:
        if home and (Path(home) / 'include' / 'jni.h').exists():
            return home
    return None


def run_cooja(unit):
    suite = TESTBED / unit['suite_directory']
    stem = unit['scenario'][:-len('.csc')]
    # Current Cooja writes COOJA.testlog; Ant-era simexec leaves the verdict in
    # <scenario>.testlog (and the script log in <scenario>.<seed>.scriptlog).
    for old in [*suite.rglob('COOJA.testlog'), *suite.glob(stem + '.testlog'),
                *suite.glob(stem + '.*.scriptlog'), *suite.glob(stem + '.*.coojalog')]:
        old.unlink()
    home = cooja_java_home()
    java_home = {'JAVA_HOME': home} if home else {}
    # A 32-bit JVM sizes its default heap from the host's memory; on a large
    # node that reservation takes the address the class-data archive needs, and
    # the era's simexec.sh forces -Xshare:on ("Unable to use shared archive").
    # 512 MB is the heap Ant-era Cooja's own build files use.
    java_options = {}
    if platform.architecture()[0] == '32bit':
        java_options['JAVA_TOOL_OPTIONS'] = (os.environ.get('JAVA_TOOL_OPTIONS', '') + ' -Xmx512m').strip()
    env = {**os.environ, **java_home, **java_options,
           'GRADLE_USER_HOME': str(TESTBED / '.embedeval' / 'gradle')}
    cmd = ['make', '-C', str(suite), 'TESTS=' + unit['scenario'],
           'BASESEED=1', 'RUNCOUNT=1', 'NUMCORES=4']
    rc, output = run_logged(cmd, log_path(unit, 'run'), env=env)
    logs = list(suite.rglob('COOJA.testlog')) + list(suite.glob(stem + '.testlog'))
    verdict = parse_cooja('\n'.join(p.read_text(errors='replace') for p in logs))
    if COOJA_LOAD_FAILURE.search(output):
        verdict, compile_failed = 'not_run', True
    else:
        compile_failed = cooja_build_failed(rc, verdict, output)
    if rc == 'timeout' and verdict == 'not_run':
        verdict = 'timeout'
    elif rc != 0 and verdict == 'passed':
        verdict = 'error'
    return {unit['id']: verdict}, verdict != 'not_run', compile_failed


def run_script(unit):
    suite = TESTBED / unit['suite_directory']
    summary = suite / 'summary'
    if summary.exists():
        summary.unlink()
    cmd = ['make', '-C', str(suite),
           'EXAMPLES=' + unit['entry']]
    rc, output = run_logged(cmd, log_path(unit, 'run'))
    summary_text = summary.read_text(errors='replace') if summary.exists() else ''
    verdict = parse_script(output, unit['entry'], summary_text)
    if rc == 'timeout' and verdict == 'not_run':
        verdict = 'timeout'
    elif rc != 0 and verdict == 'passed':
        verdict = 'error'
    return {unit['id']: verdict}, verdict != 'not_run'


def main():
    cfg = json.loads(CONFIG.read_text())
    LOGS.mkdir(parents=True, exist_ok=True)
    build_only = '--build-only' in sys.argv[1:]
    statuses = {}
    build_failed = False
    incomplete = False
    for unit in cfg['units']:
        kind = unit['kind']
        if kind == 'native':
            if not build_native(unit):
                print('{}: native build failed; see {}'.format(unit['id'], log_path(unit, 'build')))
                build_failed = True
                statuses.update({unit['id'] + '::' + case['id']: 'not_run'
                                 for case in unit.get('cases', [])})
                continue
            if not build_only:
                result, complete = run_native(unit)
                statuses.update(result)
                incomplete |= not complete
        elif build_only:
            print('{}: {} builds when run; not precompiled'.format(unit['id'], kind))
        elif kind == 'cooja':
            result, complete, compile_failed = run_cooja(unit)
            statuses.update(result)
            incomplete |= not complete
            if compile_failed:
                print('{}: Cooja build failed; see {}'.format(unit['id'], log_path(unit, 'run')))
                build_failed = True
        elif kind == 'script':
            result, complete = run_script(unit)
            statuses.update(result)
            incomplete |= not complete
        else:
            raise ValueError('unknown unit kind: ' + kind)
    if build_only:
        return 2 if build_failed else 0
    if not cfg.get('observe'):
        expected = cfg.get('after_required', cfg.get('fail_to_pass', []) + cfg.get('pass_to_pass', []))
        for name in expected:
            statuses.setdefault(name, 'not_run')
    print('\n--- contiki-ng: results')
    for name, status in statuses.items():
        print('  {} {}'.format(status, name))
    print('--- end ---')
    if build_failed:
        print('==> run_tests: Build failure')
        return 2
    if any(status in ('failed', 'error', 'timeout') for status in statuses.values()):
        return 1
    return 0 if statuses and all(s == 'passed' for s in statuses.values()) and not incomplete else 3


if __name__ == '__main__':
    sys.exit(main())
