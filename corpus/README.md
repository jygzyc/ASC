# Acceptance corpora

The acceptance suite (`tests/acceptance/scenarios.json`) pins these archives by
SHA-256. They are not shipped with the repository — drop copies into this directory
to unblock the scenarios that use them (`python3 bench/acceptance.py --allow-blocked`
tolerates their absence).

```
corpus/fixture.apk                     sha256 aabb2df39cd7b5f72c7314514c08e181c302c9b3b73dc4205906841ac2766e9d
corpus/com.tencent.mm/base.apk         sha256 ec7ff6d8cee2f5ae684c3e02a16f5b4b528d10d5aa3ca0bc7ffa503240ff922c
corpus/com.android.settings/base.apk   sha256 e6777c26b5f271dc7065766d158f51574c6b94c960c6631706a788b2d936426d
corpus/services.jar                    sha256 772f8988f5487692ebe9febf5ea67253e680563e447064655d8e33c9345c4fc6
```

`fixture.apk` is a small single-DEX APK (6,220 classes); any equivalent fixture can
replace it as long as the scenarios pinned to its classes still find them (update the
hash in `scenarios.json` accordingly). The other three are the quality/contract
corpora; their scenario budgets are calibrated to these exact builds.
