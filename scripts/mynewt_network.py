"""Network isolation for Mynewt containers: agent runs, validation and grading.

Every command runs in a fresh network namespace that has only loopback, so
nothing inside the container can reach GitHub (the fix commit, the PR page) or
any other host. An instance whose tests need a network adds the source-recorded
private veth fixture (metadata "network_fixture": "mynewt-veth"); it is local
to the namespace too.
"""
import shlex

LOOPBACK = 'set -e\nip link set lo up\n'
SETUP = 'set -e\nip link set lo up\nip link add bench0 type veth peer name bench1\nip address add 192.0.2.1/24 dev bench0\nip -6 address add fe80::1/64 dev bench0 nodad\nip -6 address add fd00::1/64 dev bench0 nodad\nip address add 192.0.2.2/24 dev bench1\nip -6 address add fe80::2/64 dev bench1 nodad\nip -6 address add fd00::2/64 dev bench1 nodad\nip link set bench0 up\nip link set bench1 up\nip -6 route add ff00::/8 dev bench1\nip -brief address\nip -6 route\n'


def isolated_command(command: list[str], fixture: str | None) -> list[str]:
    """`command` in its own network namespace: loopback only, plus the veth
    pair when `fixture` is "mynewt-veth"."""
    if fixture not in (None, "mynewt-veth"):
        raise ValueError(f"unsupported Mynewt network fixture: {fixture}")
    setup = SETUP if fixture else LOOPBACK
    return ["unshare", "--user", "--map-root-user", "--net", "env", "-u", "BASH_ENV", "-u", "ENV", "bash", "--noprofile", "--norc", "-c",
            "export PATH=/usr/sbin:/sbin:$PATH\n{\n" + setup + "} >/dev/null\nexec " + shlex.join(command)]
