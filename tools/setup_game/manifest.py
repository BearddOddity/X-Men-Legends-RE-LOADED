"""Known-good dumps this port targets.

Keyed by SHA-256 of `default.xbe`. The user supplies their own disc; this table
only says which dumps we have actually verified the recompiler against. It
contains no game data - a hash is not a copy.

Adding an entry is a claim that the pipeline was *run* against that dump, not
that it ought to work. An unverified region will recompile happily and then
misbehave in ways that look like lifter bugs, which is exactly the confusion
this table exists to prevent.
"""

SUPPORTED = {
    "2ea531f11e0b5b7ca651485012b871ef99a5099c566a6d7cb8ec327ee73d62ac": {
        "label": "X-Men Legends (World) [retail]",
        "title_id": 0x4156001E,
        "region": 0x7,          # NTSC-U | NTSC-J | PAL, i.e. the World release
        "xdk": "1.0.5849",
        "size": 4698112,
        "notes": "The dump the recompiler is developed against.",
    },
}

# Title IDs we recognise but have no verified dump for. Lets us tell
# "wrong region of the right game" apart from "completely wrong disc".
KNOWN_TITLE_IDS = {
    0x4156001E: "X-Men Legends",
}
