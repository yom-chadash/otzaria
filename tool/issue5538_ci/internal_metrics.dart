class Issue5538Metrics {
  static final clock = Stopwatch()..start();
  static final queuedAt = <Object, int>{};
  static final counters = <String, int>{};
  static final agesUs = <int>[];

  static void count(String key, [int value = 1]) {
    counters[key] = (counters[key] ?? 0) + value;
  }

  static void enqueue(Object task, int depth, {bool continuation = false}) {
    queuedAt[task] = clock.elapsedMicroseconds;
    count(continuation ? 'continuations' : 'enqueued');
    final old = counters['maxQueueDepth'] ?? 0;
    if (depth > old) counters['maxQueueDepth'] = depth;
  }

  static void selected(Object task, int started, int canceled, int ready) {
    final now = clock.elapsedMicroseconds;
    count('selections');
    count('canceledProbes', canceled);
    count('readyProbes', ready);
    count('selectionWallUsInstrumented', now - started);
    final queued = queuedAt.remove(task);
    if (queued != null) agesUs.add(now - queued);
  }

  static Map<String, Object?> snapshot({bool reset = false}) {
    final result = <String, Object?>{
      'counters': Map<String, int>.of(counters),
      'queueAgesUs': List<int>.of(agesUs),
      'trackedQueuedTasks': queuedAt.length,
    };
    if (reset) {
      counters.clear();
      agesUs.clear();
    }
    return result;
  }
}
