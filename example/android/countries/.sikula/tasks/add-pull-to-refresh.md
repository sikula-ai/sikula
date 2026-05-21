# Pull-to-Refresh on Countries List

## Background

The countries list screen loads data on launch and shows a retry button on error.
There is no way to refresh the list once it has loaded successfully.

## Requirements

- The user can pull down on the countries list to trigger a refresh.
- While refreshing, the pull indicator is visible at the top of the list.
- The full-screen loading spinner is shown only on the initial load, not during a pull-to-refresh.
- On error, the existing error state and retry button are unchanged.

## Out of scope

- Pull-to-refresh on any screen other than the countries list
- Caching or debouncing refresh calls
