## [1.13.2](https://github.com/mini-app-polis/rotator-cog/compare/v1.13.1...v1.13.2) (2026-10-28)


### Bug Fixes

* **doc-004:** drop CLOUDFLARE_TURNSTILE_TOKEN_PARAMETER from .env.example ([3a86d47](https://github.com/mini-app-polis/rotator-cog/commit/3a86d47199c8124a656f45b35cbf9f614bde6e80))

## [1.13.1](https://github.com/mini-app-polis/rotator-cog/compare/v1.13.0...v1.13.1) (2026-10-28)


### Bug Fixes

* **doc-004:** list the sweep-tasks toggle and Turnstile rotation settings in .env.example; mark rotate_secret no-retry (PIPE-007) ([c60d6b5](https://github.com/mini-app-polis/rotator-cog/commit/c60d6b5e7d2aac8e7d5ef2d61557b9e39f0aab6d))

# [1.13.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.12.0...v1.13.0) (2026-10-27)


### Bug Fixes

* **inventory:** drop kwalla-dance's deploy hook, which does not exist ([dfbca74](https://github.com/mini-app-polis/rotator-cog/commit/dfbca744dc41fd16e0e7378ef67721c3f48b6a22))
* **inventory:** hold the Spotify refresh token outside the vault and narrow its write token ([795d304](https://github.com/mini-app-polis/rotator-cog/commit/795d304cedb3ba2d15a321826b76936b9c046f49))
* **inventory:** keep the rotator's Railway tokens in its own Doppler config ([98d746b](https://github.com/mini-app-polis/rotator-cog/commit/98d746baf53083c5588de27f49cf944f41a6ea47))
* **worker:** stop requiring an empty previous slot on Railway before a rotation ([c10c1b7](https://github.com/mini-app-polis/rotator-cog/commit/c10c1b7b49e784482c6cf55f07bf48c2675d756e))


### Features

* **worker:** rotate TURNSTILE_SECRET_KEY through Cloudflare's rotate-secret API ([9afa4f3](https://github.com/mini-app-polis/rotator-cog/commit/9afa4f3b307cabdd4d51a6637ffc0990759dc172))

# [1.12.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.11.0...v1.12.0) (2026-10-27)


### Features

* write rotations to Doppler alone and let its syncs deliver the GitHub copies ([94f4f23](https://github.com/mini-app-polis/rotator-cog/commit/94f4f2365135ebc4c1ae5551d4eeabfd9a782a61))

# [1.11.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.10.0...v1.11.0) (2026-10-27)


### Features

* **doppler:** empty a previous slot instead of deleting it, so service configs can reference it ([cc6fca5](https://github.com/mini-app-polis/rotator-cog/commit/cc6fca5a15b335bca5afe299541415ac90470898))

# [1.10.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.9.0...v1.10.0) (2026-10-26)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.30.0 ([ed457bb](https://github.com/mini-app-polis/rotator-cog/commit/ed457bb39ca2b71149dda14d34309b20ff38964d))


### Features

* **sweep:** flag Doppler secret changes that no recorded rotation explains ([5597045](https://github.com/mini-app-polis/rotator-cog/commit/5597045e079f63e732c69e70ab79d88aac67e317))

# [1.9.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.8.3...v1.9.0) (2026-10-26)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.29.0 ([0bca232](https://github.com/mini-app-polis/rotator-cog/commit/0bca2328c4ac5dd789b8d901df02997d636e6c93))


### Features

* **sweep:** ask for one task per due manual secret, naming the rotate.py command ([3bd1216](https://github.com/mini-app-polis/rotator-cog/commit/3bd12166fef5c29ed39a2c3ad183913cb42a636b))

## [1.8.3](https://github.com/mini-app-polis/rotator-cog/compare/v1.8.2...v1.8.3) (2026-10-26)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.28.0 ([fb27ece](https://github.com/mini-app-polis/rotator-cog/commit/fb27ece622b3c6ac5819adb90b4654bc214e9fc9))
* **record:** record a rotation with no automatic check as unchecked and count it as rotated ([4455e6e](https://github.com/mini-app-polis/rotator-cog/commit/4455e6e513575206e5147536a6522c69ff2d367b))

## [1.8.2](https://github.com/mini-app-polis/rotator-cog/compare/v1.8.1...v1.8.2) (2026-10-26)


### Bug Fixes

* **sweep:** list every Doppler credential, scope consumers per config, and make check agree with the sweep ([f4c2a66](https://github.com/mini-app-polis/rotator-cog/commit/f4c2a66f1cade8e9e1d3250fd8384119e7759c72))

## [1.8.1](https://github.com/mini-app-polis/rotator-cog/compare/v1.8.0...v1.8.1) (2026-10-26)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.27.0 ([3bd7bfa](https://github.com/mini-app-polis/rotator-cog/commit/3bd7bfa845c68e083dcf82d1944c6d7bccba39d8))

# [1.8.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.7.2...v1.8.0) (2026-10-25)


### Features

* **sweep:** pass over a key the services are not ready for and rotate the next most overdue ([031e7ef](https://github.com/mini-app-polis/rotator-cog/commit/031e7efe67d4978c89e7bc2bc216f0a4c85f00f3))

## [1.7.2](https://github.com/mini-app-polis/rotator-cog/compare/v1.7.1...v1.7.2) (2026-10-25)


### Bug Fixes

* **worker:** retry Doppler, Railway and whoami calls and validate their answers; document env, decisions and local runs ([2b672c7](https://github.com/mini-app-polis/rotator-cog/commit/2b672c719d8a85ddfc28d41087e93121560fbe9f))

## [1.7.1](https://github.com/mini-app-polis/rotator-cog/compare/v1.7.0...v1.7.1) (2026-10-25)


### Bug Fixes

* **worker:** strip whitespace from tokens read from Parameter Store ([f65dc98](https://github.com/mini-app-polis/rotator-cog/commit/f65dc98bcae99fce3e105eb5fd60791f376c7176))

# [1.7.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.6.0...v1.7.0) (2026-10-25)


### Features

* **sweep:** rotate due keys only when ROTATOR_SWEEP_ROTATE is true; report them otherwise ([b22bdc9](https://github.com/mini-app-polis/rotator-cog/commit/b22bdc97cf186042bec05394dd711d13dc9f1502))

# [1.6.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.5.0...v1.6.0) (2026-10-25)


### Features

* **worker:** rotate IAM access keys, deleting the old key only once the new one verifies ([f858935](https://github.com/mini-app-polis/rotator-cog/commit/f858935802ad36b5bda1c40afbf4494170b8d4e0))

# [1.5.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.4.0...v1.5.0) (2026-10-25)


### Features

* **sweep:** retire previous keys nothing uses, rotate the most overdue key and report drift, daily per config ([f7f82f6](https://github.com/mini-app-polis/rotator-cog/commit/f7f82f635d9641ef9d1bb9451e7ea45b2a0bb28c))

# [1.4.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.3.0...v1.4.0) (2026-10-25)


### Features

* **worker:** verify the new key with whoami and record each rotation as ok or failed ([5062e50](https://github.com/mini-app-polis/rotator-cog/commit/5062e50418ccd4d6804f44f97b83148c59e44401))

# [1.3.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.2.0...v1.3.0) (2026-10-25)


### Features

* **worker:** redeploy the secret's Railway services once Railway shows the new value, reusing their last image ([95aa8c6](https://github.com/mini-app-polis/rotator-cog/commit/95aa8c6a912077f74d99527ba7e5513c1c568204))

# [1.2.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.1.0...v1.2.0) (2026-10-25)


### Bug Fixes

* **secrets:** list the rotator's Railway project tokens per environment ([fafdafe](https://github.com/mini-app-polis/rotator-cog/commit/fafdafea0f52f692d1d9ec223bc474fd6d57d92f))


### Features

* **worker:** mint the new value and hand it over in Doppler in two phases, refusing secrets also held in GitHub ([7f82292](https://github.com/mini-app-polis/rotator-cog/commit/7f82292d2c1fd7f427259d6051926289858aa2dc))

# [1.1.0](https://github.com/mini-app-polis/rotator-cog/compare/v1.0.0...v1.1.0) (2026-10-25)


### Bug Fixes

* **secrets:** print secret names again; the CodeQL findings were names, not values ([dc4af84](https://github.com/mini-app-polis/rotator-cog/commit/dc4af847c9b20c0bf5104afb3a6e3ed323db4275))


### Features

* **secrets:** record rotations through the API and import the legacy ledger ([07df008](https://github.com/mini-app-polis/rotator-cog/commit/07df008210b07c4e61ecc3737b5b1964d1eceb83))
* **worker:** Lambda worker skeleton that checks rotation jobs against the inventory and reads the config's Doppler token ([e282de1](https://github.com/mini-app-polis/rotator-cog/commit/e282de122c70f12d275349df3f427960dbef5c5e))

# 1.0.0 (2026-10-25)


### Bug Fixes

* **cogs:** evaluator and groomer read the fleet's one GitHub token ([698ad3c](https://github.com/mini-app-polis/rotator-cog/commit/698ad3c6f56c77e8383d995fe7d1de5769a36791))
* **secrets:** development keys carry the dev_ prefix ([f1749bb](https://github.com/mini-app-polis/rotator-cog/commit/f1749bb88fb0f21789dc764056f06c328e026d49))
* **secrets:** ledger keeps only an AWS key id's last four; one Google and one Brevo variable ([be8f0ab](https://github.com/mini-app-polis/rotator-cog/commit/be8f0aba62e25f66762cf247fb95380172eadc3e))
* **secrets:** list matches GitHub-only secrets in the ledger; record the Spotify re-auth ([32e4fe7](https://github.com/mini-app-polis/rotator-cog/commit/32e4fe79a401a0922dccb1efea6563f4b33e0ac4))


### Features

* **secrets:** generated secrets are due every 180 days ([cb06e59](https://github.com/mini-app-polis/rotator-cog/commit/cb06e593f5ac28db8e0251b682200992369efbbd))
* **secrets:** inventory, rotation script and compromise runbook ([fdee910](https://github.com/mini-app-polis/rotator-cog/commit/fdee910ae8aeed898c1a65266f8ce43d4fa38654))
* **secrets:** verify and pg-roles commands; record Railway incident rotations ([5c26030](https://github.com/mini-app-polis/rotator-cog/commit/5c26030c0293099712e364fc0382df0da767def0))
* **secrets:** verify command; record Railway incident rotations ([61055ad](https://github.com/mini-app-polis/rotator-cog/commit/61055ade2a540dc210041e1fc1eb9c1c3cd75410))
