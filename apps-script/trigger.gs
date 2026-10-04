/**
 * Sends one repository_dispatch event to mzaiger/TrendingTopics. All three workflows
 * (social media, Google, Reddit) listen for it, so one call refreshes all three JSON files.
 *
 * Setup:
 *   1. Project Settings (gear icon) > Script properties > add GITHUB_TOKEN = your token.
 *   2. Triggers (clock icon) > Add Trigger > triggerTrendingTopicsWorkflow > Time-driven >
 *      Hour timer > Every hour. (The workflows have no schedule of their own.)
 */

// Hours (0-23, in the timezone below) to skip so the Apify free credit lasts the month.
// 4 skipped hours = 20 runs a day. Use [] to run every hour.
const SKIP_HOURS = [2, 3, 4, 5];
const TIMEZONE = "America/Chicago";

function triggerTrendingTopicsWorkflow() {
  const hour = parseInt(Utilities.formatDate(new Date(), TIMEZONE, "H"), 10);
  if (SKIP_HOURS.indexOf(hour) !== -1) {
    Logger.log("Skipping hour " + hour + " (SKIP_HOURS).");
    return;
  }

  const token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  const url = "https://api.github.com/repos/mzaiger/TrendingTopics/dispatches";

  const options = {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + token,
      "Accept": "application/vnd.github.v3+json",
      "User-Agent": "Google-Apps-Script-Automation"
    },
    contentType: "application/json",
    // Both names are accepted by the workflows ("trigger-expiration-pull" is the old one).
    payload: JSON.stringify({ event_type: "trigger-trending-pull" }),
    muteHttpExceptions: true
  };

  try {
    const res = UrlFetchApp.fetch(url, options);
    const code = res.getResponseCode();
    if (code === 204) {
      Logger.log("TrendingTopics workflows dispatched successfully!");
    } else {
      Logger.log("GitHub answered " + code + ": " + res.getContentText());
    }
  } catch (e) {
    Logger.log("Failed to dispatch workflow: " + e.toString());
  }
}
