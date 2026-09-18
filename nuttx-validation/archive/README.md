# Archived per-PR NuttX validation scripts

Sixteen hand-written scripts, one per candidate PR, used before a general
NuttX build path existed. Each hardcodes its own commits, image name and build
steps; several reference candidates that never became instances.

Superseded by:

    python scripts/build_instances.py --instance nuttx__nuttx-<PR>
    python scripts/validate_instance.py nuttx__nuttx-<PR>

Kept because they are the only record of how the six NuttX instances were
originally validated.
