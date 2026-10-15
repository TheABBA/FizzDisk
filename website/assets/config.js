// FizzDisk site config — edit before go-live.
//   BUY_URL = your 2Checkout product checkout URL. Leave "" to trial the key
//   page (success.html) until checkout is set up.
//   DOWNLOAD_URL = path to the installer you upload per release.
window.FIZZ_CONFIG = {
  VERSION: "1.1.6",
  PRICE: "$65",
  BUY_URL: "",
  DOWNLOAD_URL: "download/FizzDiskSetup.exe",
  // Set DOWNLOAD_AVAILABLE to false while a release is being finalized: the
  // Download buttons show a "new build coming" note instead of delivering the exe.
  DOWNLOAD_AVAILABLE: true,
  DOWNLOAD_NOTE: "A refreshed build is being finalized \u2014 it'll be available here shortly.",
  // Set BUY_AVAILABLE to false to disable the Buy buttons (no checkout yet).
  BUY_AVAILABLE: false,
  BUY_NOTE: "Purchases open soon \u2014 FizzDisk is being finalized; check back shortly.",
  SUCCESS_URL: "success.html",
};
