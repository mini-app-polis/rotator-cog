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
