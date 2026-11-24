// Shared only by the copied fork observer and its private viewer overlay.
class Issue5538LayoutCounters {
  static const marker = 'm02-layout-counts-v1';
  static int indexBuilds = 0;
  static int indexBuildRects = 0;
  static int matchCalls = 0;
  static int rectChecks = 0;

  static void reset() {
    indexBuilds = indexBuildRects = matchCalls = rectChecks = 0;
  }

  static Map<String, int> snapshot() => {
    'indexBuilds': indexBuilds,
    'indexBuildRects': indexBuildRects,
    'matchCalls': matchCalls,
    'rectChecks': rectChecks,
  };
}
