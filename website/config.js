// FizzDisk website configuration -- edit these before going live.
//
// 1. BUY_URL: replace with your Stripe Payment Link (or Checkout URL).
//    HTTPS checkout automatically pauses at "Success URL" after paying, so
//    no backend is required -- the key is generated on success.html.
// 2. If the app version/price changes, update VERSION / PRICE here too.
window.FIZZ_CONFIG = {
  VERSION: "1.0.0",
  PRICE: "$65",
  // Stripe hosted checkout / payment link -> paste your URL here:
  BUY_URL: "PASTE_YOUR_STRIPE_PAYMENT_LINK_HERE",
  // Where the installer lives relative to this page (upload per release):
  DOWNLOAD_URL: "download/FizzDiskSetup.exe",
  // Where the checkout redirects the buyer after a successful payment:
  SUCCESS_URL: "success.html",
};
