// Imported only by the explicitly patched, fork-only count-probe package.
class Issue5538WrapMetrics {
  static bool enabled = false;
  static final Stopwatch _clock = Stopwatch()..start();
  static final List<Map<String, Object?>> _rows = [];

  static void reset({required bool collect}) {
    enabled = collect;
    _rows.clear();
  }

  static int begin() => enabled ? _clock.elapsedMicroseconds : -1;

  static void finish({
    required int started,
    required int page,
    required int beforeLength,
    required int added,
    required int insertAt,
    required int afterLength,
    required int? indexBefore,
    required int? indexAfter,
  }) {
    if (started < 0) return;
    final elapsed = _clock.elapsedMicroseconds - started;
    final copied = added > 0 && insertAt != beforeLength;
    _rows.add({
      'pageNumber': page,
      'beforeLength': beforeLength,
      'addedMatches': added,
      'insertAt': insertAt,
      'afterLength': afterLength,
      'copiedExistingReferences': copied ? beforeLength : 0,
      'newListReferences': copied ? afterLength : 0,
      'allocatedNewResultList': copied,
      'currentIndexBefore': indexBefore,
      'currentIndexAfter': indexAfter,
      'instrumentedInsertionUs': elapsed,
    });
  }

  static List<Map<String, Object?>> take() => List.of(_rows);
}
