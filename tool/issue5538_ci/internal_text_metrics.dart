class Issue5538TextMetrics {
  static final rows = <Map<String, Object?>>[];

  static void record({
    required int pageNumber,
    required int rawBuildUs,
    required int normalizationUs,
    required int cleanupUs,
    required int workerBodyUs,
    required bool completed,
    required bool normalizeHebrew,
  }) => rows.add({
    'pageNumber': pageNumber,
    'workerRawBuildUs': rawBuildUs,
    'workerNormalizationUs': normalizationUs,
    'workerCleanupUs': cleanupUs,
    'workerBodyUs': workerBodyUs,
    'completed': completed,
    'normalizeHebrew': normalizeHebrew,
  });

  static List<Map<String, Object?>> take({required bool reset}) {
    final result = List<Map<String, Object?>>.of(rows);
    if (reset) rows.clear();
    return result;
  }
}
