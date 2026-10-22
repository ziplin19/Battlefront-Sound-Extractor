# Changelog

## 1.0.0-dev1

- Renamed the project to **Battlefront Sound Extractor**.
- Updated the project description to identify Battlefront (2004) and Battlefront II (2005) as target games.
- Made the default output directory consistently sit beside the source file and use its filename without the extension.
- Added validation for implausibly small ADPCM block sizes.
- Added regression tests for English-only interface, default output paths, WAV writing, and empty ADPCM input.
- Added an English README with usage instructions and known limitations.
- Removed the localization layer and translated source comments and docstrings into English so contributors can work from one language.
- Preserved the existing audio scanning and decoding implementation.

This is a development release. Real-file audio regression testing against representative files from both games is still required before declaring full support for both titles.

## Verification note

- `py_compile`: passed.
- Automated regression tests: 4 passed.
- Real-file smoke test: the supplied `shell.lvl` completed in `auto` mode and produced 22 sound files.
- The full ADPCM decode run exceeded the available test time, and no Battlefront (2004) sample was available for a separate compatibility test. Full compatibility with both games is therefore not yet verified.
