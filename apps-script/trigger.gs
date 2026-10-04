/**
 * Starts the TrendingTopics workflows on GitHub (repo mzaiger/TrendingTopics).
 *
 * Setup:
 *   1. Project Settings (gear icon) > Script properties > add GITHUB_TOKEN = your token.
 *   2. Triggers (clock icon) > Add Trigger:
 *        - triggerTrendingTopicsWorkflow  > Time-driven > Hour timer > Every hour
 *        - (optional) triggerFeedsOnly    > Time-driven > Minutes timer > Every 15 or 30 minutes
 *      The workflows have no schedule of their own, so nothing runs unless these triggers exist.
 */

// Hours (0-23, in the timezone below) when the SOCIAL MEDIA workflow is skipped so the Apify free
// credit lasts the month (4 skipped hours = 20 runs a day). Google + Reddit still run in those
// hours because they cost nothing. Use [] to run everything every hour.
const SKIP_HOURS = [2, 3, 4, 5];
const TIMEZONE = "America/Chicago";

// "trigger-trending-pull" starts all three workflows; "trigger-feeds-pull" starts Google + Reddit only.
// ("trigger-expiration-pull", the old name, still works too.)
function dispatch_(eventType) {
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
    payload: JSON.stringify({ event_type: eventType }),
    muteHttpExceptions: true
  };
  try {
    const res = UrlFetchApp.fetch(url, options);
    const code = res.getResponseCode();
    if (code === 204) {
      Logger.log("Dispatched '" + eventType + "' successfully!");
    } else {
      Logger.log("GitHub answered " + code + " for '" + eventType + "': " + res.getContentText());
    }
  } catch (e) {
    Logger.log("Failed to dispatch '" + eventType + "': " + e.toString());
  }
}

function triggerTrendingTopicsWorkflow() {
  const hour = parseInt(Utilities.formatDate(new Date(), TIMEZONE, "H"), 10);
  if (SKIP_HOURS.indexOf(hour) !== -1) {
    Logger.log("Hour " + hour + " is in SKIP_HOURS: refreshing Google + Reddit only.");
    dispatch_("trigger-feeds-pull");
    return;
  }
  dispatch_("trigger-trending-pull");
}

// Optional: Google's feed only holds its 10 newest trends, so polling it more often than hourly
// gives a fuller day's list. Add a separate 15- or 30-minute time trigger for this function.
function triggerFeedsOnly() {
  dispatch_("trigger-feeds-pull");
}
