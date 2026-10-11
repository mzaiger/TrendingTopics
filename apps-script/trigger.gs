/**
 * Starts the TrendingTopics workflows on GitHub (repo mzaiger/TrendingTopics).
 *
 * Setup:
 *   1. Project Settings (gear icon) > Script properties > add GITHUB_TOKEN = your token.
 *   2. Triggers (clock icon) > Add Trigger:
 *        - triggerTrendingTopicsWorkflow > Time-driven > Minutes timer > Every 30 minutes
 *      (Replace the old "every hour" trigger with this one.) The workflows have no schedule of their
 *      own, so nothing runs unless this trigger exists.
 *
 * How it runs: in the first half of each hour (minute 0-29) all three workflows start; in the second
 * half only the social media one does ("trigger-social-pull"). Google and Reddit therefore refresh
 * hourly. The social media workflow decides for itself whether a run is due (schedule.py): every 30
 * minutes when everything is on the cheap options, slower when Reddit or Twitter have fallen back to a
 * dearer Apify actor, so the month stays under Apify's free $5. A tick that is not due costs nothing.
 */

const TIMEZONE = "America/Chicago";   // only used to read the minute, so any timezone works

// "trigger-trending-pull" starts all three workflows; "trigger-feeds-pull" starts Google + Reddit only;
// "trigger-social-pull" starts the social media workflow only.
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
  const minute = parseInt(Utilities.formatDate(new Date(), TIMEZONE, "m"), 10);
  dispatch_(minute < 30 ? "trigger-trending-pull" : "trigger-social-pull");
}

// Optional: Google's feed only holds its 10 newest trends, so polling it more often than hourly
// gives a fuller day's list. Add a separate 15- or 30-minute time trigger for this function.
function triggerFeedsOnly() {
  dispatch_("trigger-feeds-pull");
}
