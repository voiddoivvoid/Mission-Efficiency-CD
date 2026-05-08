# MissionEff Custom Builder Source

## Layout

```text
MissionEff_Custom_Builder/
  _internals/
    MisssionEff_Template_and_Example.field.json
    paz_crypto.py
    paz_parse.py
    paz_unpack.py
  MissionEff_Custom_Builder.py
  build_exe.bat
  requirements.txt
  output/
```

## What it does

- Reads `_internals/MisssionEff_Template_and_Example.field.json`.
- Verifies every template intent only changes one 4-byte value.
- Infers the template multiplier, normally `x2`.
- Generates a new DMM Format 3 `field.json` from the multiplier entered in the UI.
- Verifies every generated change before writing the output.
- Optionally checks the current game `skill.pabgb` if the file can be found or extracted.

## Build

Run:

```bat
build_exe.bat
```

The compiled app will be created in:

```text
dist\MissionEff_Custom_Builder
```

Keep the generated `_internals` folder exposed next to the EXE. Do not bundle everything into a one-file EXE. This is intentional to reduce Defender quarantine/false-positive behavior.

## Template name

The app targets:

```text
_internals\MisssionEff_Template_and_Example.field.json
```

That spelling matches the requested name. If you rename it, keep both `Template` and `Example` in the filename.

## Optional game verification

The app checks:

```text
C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert
```

or the path in environment variable:

```text
CRIMSON_DESERT_DIR
```

If no readable `skill.pabgb` is found, the app still builds normally.
