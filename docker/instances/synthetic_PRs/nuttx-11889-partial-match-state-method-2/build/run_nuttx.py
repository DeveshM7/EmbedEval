#!/usr/bin/env python3
"""Run complete ostest inside the real NuttX simulator, without trusting early NSH return."""
import os,re,selectors,signal,subprocess,sys,time
p=subprocess.Popen(['./nuttx'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
p.stdin.write(b'ostest\npoweroff\n');p.stdin.flush()
sel=selectors.DefaultSelector();sel.register(p.stdout,selectors.EVENT_READ)
output=b'';deadline=time.monotonic()+600;complete=False
try:
 while time.monotonic()<deadline:
  ready=sel.select(0.25)
  if ready:
   data=os.read(p.stdout.fileno(),65536)
   if not data:break
   output+=data;sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
   if b'user_main: Exiting' in output:complete=True;break
  elif p.poll() is not None:break
finally:
 try:os.killpg(p.pid,signal.SIGTERM)
 except ProcessLookupError:pass
 try:p.wait(timeout=5)
 except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
 sel.close()
text=output.decode(errors='replace')
failed=bool(re.search(r'^FAIL: ',text,re.M))
print('\n%s: full_ostest'%('PASS' if complete else 'FAIL'),flush=True)
if not complete and time.monotonic()>=deadline:sys.exit(2)
sys.exit(1 if failed or not complete else 0)
