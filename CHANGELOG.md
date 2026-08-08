# Changelog

All notable changes to this project will be documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- “Old Computer → AI Lab” public-facing identity for authorized research.
- GitHub CI, contribution guidance, responsible-use guidance, security policy,
  and structured issue templates.
- Per-site Windows template configuration and a `--template-vmid` override.

### Fixed

- Windows 2022 installs now select the `2k22` VirtIO driver directory by
  default instead of inheriting the Windows 2025 default.
- Console-share tests now close HTTP responses and server sockets cleanly.

## 0.1.0 - 2026-08-02

Initial beta release of the leased, auditable Proxmox lab controller.
