# Nikon ZR settings profile

the owner's ZR menu settings, kept as Nikon's own `NCSET***.BIN` file so every rental ZR can load them in one step. The runbook is `Wiki/Operations/Nikon ZR Settings Profile.md`.

```bash
python scripts/zr_settings/zr_settings.py status   # what is plugged in, is a profile saved
python scripts/zr_settings/zr_settings.py pull     # copy NCSET*.BIN off the owner's ZR (USB) or its card into profile/
python scripts/zr_settings/zr_settings.py push     # copy the profile onto a rental's card root (reader; USB is read-only on Nikon bodies)
```

- The file is written by the camera: **Setup > Save/load menu settings > Save menu settings**. A rental loads it with **Load menu settings**.
- `profile/` holds the newest file plus `profile.json` (when, from where, size). Never rename the `.BIN`.
- Not carried by the file: date/time, monitor brightness, timecode origin, storage folder, custom Picture Controls, network pairing. The wiki note lists them.
