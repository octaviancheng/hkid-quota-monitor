# Personal GitHub Issue alerts

This fork is configured to watch every HKID registration office and both regular
and extended sessions for **7–10 October 2026 inclusive**. It runs on GitHub
Actions every five minutes and opens one aggregated Issue when matching quota
appears. The Issue assigns and mentions the repository owner, allowing GitHub to
deliver the same alert by email and through GitHub Mobile.

No mailbox password, personal access token, or other repository secret is used.

## Set up the public fork

1. Fork `chen1111-a/hkid-quota-monitor` into your personal GitHub account and keep
   the repository public. Standard GitHub-hosted Actions runners are free for
   public repositories.
2. Open the fork's **Actions** tab and enable workflows if GitHub asks you to.
3. Confirm that **Settings → General → Features → Issues** is enabled.
4. Open <https://github.com/settings/notifications>. Under **Participating and
   @mentions**, enable both **Email** and **On GitHub**, and select a verified
   email address.
5. Install and sign in to GitHub Mobile. In its notification settings, enable
   push notifications for **Direct mentions** and **Assignments**.
6. In the repository, open **Actions → quota-monitor → Run workflow**, enable
   `test_notification`, and run it. The resulting Issue is explicitly labelled
   as a test and does not claim that quota was found.
7. Confirm the test appears in GitHub's notification inbox, email, and GitHub
   Mobile. Check the email spam folder and allow `notifications@github.com` if
   necessary.

Scheduled workflows are best-effort and may occasionally be queued or delayed.
The first live run reports matching quota that is already available; later runs
report newly opened quota. Repeated openings for the same office, date, and
session are limited to one alert every six hours.

## Alert contents and privacy

Each live alert Issue lists the date, office, regular/extended session, quota
level, official data timestamp, and the official booking link. Appointment
availability can disappear quickly, and this monitor does not make a booking.

Because this is a public fork, its configuration, data, workflow logs, alert
Issues, and your GitHub username are public. Do not add identity-card numbers,
booking credentials, email addresses, or other personal information.

## Change or stop monitoring

The target is stored in the `issue_alert` object in `config.json`. Dates use
`YYYY-MM-DD`; both boundaries are inclusive. Office IDs are `RHK`, `RKO`, `RTK`,
`FTO`, `TMO`, and `YLO`; session IDs are `R` and `K`.

After booking an appointment—or after 10 October 2026—open **Actions**, select
**quota-monitor**, use the `…` menu, and choose **Disable workflow**. This stops
the five-minute checks and data commits.
