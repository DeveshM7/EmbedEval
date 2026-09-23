"""Create the external Newt project without changing upstream source files."""
import json
from pathlib import Path

meta = json.loads(Path('/opt/benchmark/metadata.json').read_text())
# Keep solution revisions and provenance on the host, out of the agent image.
Path('/opt/benchmark/metadata.json').write_text(json.dumps({
    key: meta[key] for key in ('test_path', 'baseline_tests', 'fail_to_pass', 'pass_to_pass')
}))
project = Path('/project')
(project / 'repos').mkdir(parents=True)
(project / 'repos/apache-mynewt-core').symlink_to('/testbed')
(project / 'project.yml').write_text('''project.name: benchmark
project.repositories:
    - apache-mynewt-core
repository.apache-mynewt-core:
    type: github
    vers: 0.0.0
    user: apache
    repo: mynewt-core
''')
target = project / 'targets/unittest'
target.mkdir(parents=True)
(target / 'target.yml').write_text('''target.bsp: "@apache-mynewt-core/hw/bsp/native"
target.build_profile: "debug"
target.compiler: "@apache-mynewt-core/compiler/sim"
''')
pkg = 'pkg.name: targets/unittest\npkg.type: target\n'
if meta['compatibility_cflags']:
    pkg += 'pkg.cflags:\n' + ''.join('    - ' + flag + '\n' for flag in meta['compatibility_cflags'])
(target / 'pkg.yml').write_text(pkg)
