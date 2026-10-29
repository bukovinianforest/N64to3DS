# n64to3ds console agent

A small Old 3DS application that lets the host copy files, install
n64to3ds CIAs and launch them over Wi-Fi, so that hardware test builds do
not need FBI and ftpd each time.  It is a development tool; it is not part of
the generated CIAs.

## Build and first install

```bash
# toolchain environment from AGENTS.md, then:
make -C tools/console-agent        # -> build/n64to3ds-agent.cia (+ .3dsx)
```

Install `n64to3ds-agent.cia` once with FBI (title `000400000A6E7000`,
"n64to3ds agent").  Start it from the HOME Menu; the bottom screen shows the
address and port (5001).  START quits.

Later versions update themselves: `agentctl.py deploy
tools/console-agent/build/n64to3ds-agent.cia` installs over the running agent;
the new version runs after the agent is restarted.

## Use

```bash
export N64TO3DS_AGENT=192.168.1.24
A=tools/console-agent/agentctl.py
$A ping
$A deploy out/HM64-A-async.cia --launch   # upload to /cias, install, launch
$A wait --timeout 900                      # until the agent answers again
$A get /3ds/DaedalusX64/daedalus.log A.log
```

Other commands: `ls`, `put`, `get`, `rm`, `install`, `titles`, `launch`,
`exit`.  Launching ends the agent (only one application runs at a time); it
has to be started again, by hand or by a build that jumps back to it.

## Protocol (TCP 5001)

One ASCII command per line; replies are `OK [value]` or `ERR message`.

| Command | Reply |
|---|---|
| `PING` | `OK n64to3ds-agent <version> <agent title id>` |
| `LS <dir>` | `OK`, lines `F|D <size> <name>`, then `.` |
| `GET <path>` | `OK <size>` followed by the bytes |
| `PUT <path> <size>` + bytes | `OK` (written to `<path>.part`, then renamed) |
| `DEL <path>` | `OK` |
| `INSTALL <path>` | `OK <title id>` |
| `TITLES` | `OK`, installed n64to3ds title ids, then `.` |
| `LAUNCH <title id>` | `OK`, then the agent exits and the title starts |
| `EXIT` | `OK`, then the agent quits |

## Safety limits (enforced on the console)

- Files only below `/cias/`, `/3ds/DaedalusX64/` and `/3ds/n64to3ds-agent/`;
  no `..`.  `/luma/dumps/` (Luma3DS crash dumps) is readable with `LS` and
  `GET` only.
- Titles only `00040000xxxxxx00` SD applications.  An installed title is
  replaced, launched or listed only if its product code is the one n64to3ds
  generates (`CTR-P-<low 16 bits of the unique id>`); other installed titles,
  including retail games and the agent itself, are refused.
- The agent's own title may only be installed over (self-update), never
  deleted or launched.
- Reinstalling deletes the old n64to3ds installation first.  n64to3ds titles
  keep no system save data (saves are under `/3ds/DaedalusX64/SaveGames`).

## Verification status

In Azahar (Old 3DS mode): `ping`, `ls`, `put`/`get` round trip, path
refusals, `install` of a 19 MB CIA and reinstall over it, and `titles` work.
`launch` could not be checked there: Azahar needs a system HOME Menu for
application jumps.  On hardware: not yet verified.
