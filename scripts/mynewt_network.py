"""The source-recorded private veth fixture used by Mynewt validation."""
import shlex

SETUP = 'set -e\nip link set lo up\nip link add bench0 type veth peer name bench1\nip address add 192.0.2.1/24 dev bench0\nip -6 address add fe80::1/64 dev bench0 nodad\nip -6 address add fd00::1/64 dev bench0 nodad\nip address add 192.0.2.2/24 dev bench1\nip -6 address add fe80::2/64 dev bench1 nodad\nip -6 address add fd00::2/64 dev bench1 nodad\nip link set bench0 up\nip link set bench1 up\nip -6 route add ff00::/8 dev bench1\nip -brief address\nip -6 route\n'


def isolated_command(command: list[str], fixture: str | None) -> list[str]:
    if fixture is None:
        return command
    if fixture != "mynewt-veth":
        raise ValueError(f"unsupported Mynewt network fixture: {fixture}")
    return ["unshare", "--user", "--map-root-user", "--net", "env", "-u", "BASH_ENV", "-u", "ENV", "bash", "--noprofile", "--norc", "-c",
            "export PATH=/usr/sbin:/sbin:$PATH\n{\n" + SETUP + "} >/dev/null\nexec " + shlex.join(command)]
