# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-04-01

### Added
- Pre-built binaries for macOS (ARM64), Linux (x64), and Windows (x64)
- One-liner install script for macOS and Linux

### Fixed
- Guard `readline` import for Windows compatibility
- Wrap category ID validation errors in `ClickException` for cleaner output
- Drop macOS Intel build target (GitHub `macos-13` runners unavailable)

## [0.2.0] - 2026-04-01

### Added
- Locale-aware display with correct currency symbols and Audible URLs for all 9 marketplaces
- New filters: `--narrator`, `--min-hours`, `--language`, `--all-languages`, `--first-in-series`, `--skip-owned`
- Saved search profiles (`deals profile save/list/delete`, `deals find --profile`)
- Interactive browsing mode (`-i` flag) — view details, open in browser, add to wishlist
- `deals recap` command for price-drop summaries
- `deals notify` command with webhook support for deal alerts

### Fixed
- 13 security vulnerabilities addressed (P0 and P1 audit findings)

## [0.1.0] - 2026-04-01

### Added
- Initial release
- `deals find` — browse and filter deals by price and genre
- `deals search` — keyword search with filters
- `deals detail` / `deals compare` — single and side-by-side audiobook info
- `deals categories` — list and drill into Audible genres
- `deals wishlist` / `deals watch` — watchlist with price tracking
- `deals history` — per-ASIN price history with sparkline charts
- Export to JSON and CSV
- CI/CD with GitHub Actions (test matrix: Python 3.11/3.12/3.13, automated releases)

[0.3.0]: https://github.com/chauduyphanvu/audible-deals/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/chauduyphanvu/audible-deals/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.1.0
