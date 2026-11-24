const _indexVerificationPages = [2, 4];

const perfPdfMultiWindowProtocol = <String, Object?>{
  'version': 3,
  'nativeWindows': 2,
  'sources': 'distinct verified paths with identical generated PDF content',
  'primaryPages': [1, 3],
  'secondaryPages': [2, 4],
  'reopenedPage': 2,
  'indexVerificationPages': _indexVerificationPages,
  'printSourcePages': [2, 3],
  'printRows': 1,
  'printColumns': 2,
  'printWidth': 480,
  'printHeight': 320,
  'printRightToLeft': true,
  'transitionRasterPage': 2,
  'rasterInk': 'all RGB channels below 128 after compositing over white',
  'rasterAlpha': 'printing macOS premultiplied; PDFium straight',
  'nativeTransitions': ['minimizeRestore', 'closeSecondary', 'closeReopened'],
  'readiness': 'native view identity, target center and final visible image',
  'owner': 'Live published owner stable within each operation; replacements between operations retain source and reference key.',
  'close': 'Every observed owner event stream closed and native dispose awaited after detach; native view absent.',
  'imageBudgetAttribution': {
    'legacyScopeIdentity': 'current-window-ui-isolate',
    'actualScope':
        'all views sharing pdfSharedImageBudget in this Dart isolate',
    'perWindowAttribution': false,
    'peakScope': 'since-budget-construction',
  },
};

const perfPdfMultiWindowSteps = <String>[
  'open',
  'concurrentWork',
  'minimizeRestore',
  'closeSecondary',
  'reopen',
  'closeReopened',
  'teardown',
];

/// Counts visible dark RGBA/BGRA content after compositing over white.
/// Antialiased glyphs may have no opaque pixels; transparent RGB is not ink.
int perfPdfInkPixelsOnWhite(
  List<int> pixels, {
  bool premultipliedAlpha = false,
}) {
  if (pixels.length % 4 != 0) {
    throw ArgumentError(
      'Native raster must contain complete four-byte pixels.',
    );
  }
  var ink = 0;
  for (var offset = 0; offset < pixels.length; offset += 4) {
    final alpha = pixels[offset + 3];
    final white = 255 * (255 - alpha);
    final multiplier = premultipliedAlpha ? 255 : alpha;
    if (pixels[offset] * multiplier + white < 128 * 255 &&
        pixels[offset + 1] * multiplier + white < 128 * 255 &&
        pixels[offset + 2] * multiplier + white < 128 * 255) {
      ink++;
    }
  }
  return ink;
}

Map<Object?, Object?> _map(Object? value) =>
    value is Map ? value : const <Object?, Object?>{};

bool _integer(Object? value) => value is int && value >= 0;
bool _positive(Object? value) => value is int && value > 0;
bool _path(Object? value) => value is String && value.trim().isNotEmpty;
bool _hash(Object? value) =>
    value is String && RegExp(r'^[a-f0-9]{64}$').hasMatch(value);

bool _ids(Object? value) =>
    value is List &&
    value.every(_integer) &&
    value.toSet().length == value.length;

bool _pages(Object? value, List<int> expected) {
  if (value is! List || value.length != expected.length) return false;
  for (var index = 0; index < expected.length; index++) {
    if (value[index] is! int || value[index] != expected[index]) return false;
  }
  return true;
}

bool _sameReader(Object? value, Object? original) {
  final current = _map(value);
  final opened = _map(original);
  return current['viewId'] == opened['viewId'] &&
      current['nativeHandle'] == opened['nativeHandle'];
}

bool _distinctReaders(Object? first, Object? second) {
  final a = _map(first);
  final b = _map(second);
  return a['viewId'] != b['viewId'] &&
      a['nativeHandle'] != b['nativeHandle'] &&
      _map(a['nativeOwner'])['documentIdentity'] !=
          _map(b['nativeOwner'])['documentIdentity'];
}

bool _liveOwner(Object? value) {
  final owner = _map(value);
  return _positive(owner['generation']) &&
      _integer(owner['documentIdentity']) &&
      _path(owner['sourceName']) &&
      owner['sourceName'] == owner['expectedSourceName'];
}

bool _captureCheck(Object? value, Object? generation) {
  final check = _map(value);
  return _path(check['operation']) &&
      _positive(generation) &&
      check['generation'] is int &&
      check['generation'] == generation &&
      check['matchesReferenceKey'] == true &&
      check['matchesSourceName'] == true &&
      check['matchesPublishedOwner'] == true &&
      check['closedEventObserved'] == false &&
      check['currentOwner'] == true;
}

bool _ownerOperation(Object? value) {
  final data = _map(value);
  final owner = _map(data['nativeOwner']);
  final checks = data['ownerChecks'];
  if (!_liveOwner(owner) || checks is! List || checks.length != 3) {
    return false;
  }
  final pair = _map(checks[2]);
  final name = pair['operation'];
  final generation = owner['generation'];
  return _path(name) &&
      _captureCheck(checks[0], generation) &&
      _captureCheck(checks[1], generation) &&
      _map(checks[0])['operation'] == '$name:before' &&
      _map(checks[1])['operation'] == '$name:after' &&
      pair['beforeGeneration'] is int &&
      pair['beforeGeneration'] == generation &&
      pair['afterGeneration'] is int &&
      pair['afterGeneration'] == generation &&
      pair['ownerUnchangedDuringOperation'] == true;
}

bool _ownerWasClosed(Object? lifetimes, Object? value) {
  final data = _map(lifetimes);
  final owner = _map(_map(value)['nativeOwner']);
  final entries = data['owners'];
  if (!_liveOwner(owner) || entries is! List) return false;
  return entries.map(_map).where((entry) {
        return entry['generation'] == owner['generation'] &&
            entry['documentIdentity'] == owner['documentIdentity'] &&
            entry['sourceName'] == owner['sourceName'] &&
            data['expectedSourceName'] == owner['expectedSourceName'];
      }).length ==
      1;
}

bool _allOwnersClosed(Object? value) {
  final data = _map(value);
  final owners = data['owners'];
  final checks = data['checks'];
  if (data['retainsDocumentRef'] != false ||
      data['observerStopped'] != true ||
      data['allNativeClosesCompleted'] != true ||
      !_path(data['expectedSourceName']) ||
      owners is! List ||
      owners.isEmpty ||
      data['ownersObserved'] is! int ||
      data['ownersObserved'] != owners.length ||
      data['replacementsObserved'] is! int ||
      data['replacementsObserved'] != owners.length - 1 ||
      checks is! List ||
      checks.isEmpty) {
    return false;
  }
  final identities = <int>{};
  for (var index = 0; index < owners.length; index++) {
    final owner = _map(owners[index]);
    if (owner['generation'] is! int ||
        owner['generation'] != index + 1 ||
        !_integer(owner['documentIdentity']) ||
        !identities.add(owner['documentIdentity'] as int) ||
        owner['sourceName'] != data['expectedSourceName'] ||
        owner['documentEventsClosed'] != true ||
        owner['nativeDisposeAwaited'] != true) {
      return false;
    }
  }
  for (final value in checks) {
    final check = _map(value);
    final generation = check['generation'] ?? check['beforeGeneration'];
    if (!_positive(generation) || (generation as int) > owners.length) {
      return false;
    }
    if (check.containsKey('generation')) {
      if (!_captureCheck(check, generation)) return false;
    } else if (!_path(check['operation']) ||
        check['afterGeneration'] is! int ||
        check['afterGeneration'] != generation ||
        check['ownerUnchangedDuringOperation'] != true) {
      return false;
    }
  }
  return true;
}

bool _reader(Object? value, int page, Object? path) {
  final data = _map(value);
  final views = data['liveViewIds'];
  return _path(path) &&
      data['path'] == path &&
      data['targetPage'] is int &&
      data['targetPage'] == page &&
      data['visibleRenderState'] == 'ready' &&
      data['targetCenterVisible'] == true &&
      _ownerOperation(data) &&
      data['windowVisible'] == true &&
      data['windowMinimized'] == false &&
      _integer(data['viewId']) &&
      data['screenViewId'] is int &&
      data['screenViewId'] == data['viewId'] &&
      _positive(data['nativeHandle']) &&
      _ids(views) &&
      (views as List).contains(data['viewId']);
}

bool _interval(Object? value) {
  final data = _map(value);
  return _integer(data['startUs']) &&
      _integer(data['endUs']) &&
      (data['endUs'] as int) > (data['startUs'] as int);
}

bool _overlap(Object? first, Object? second) {
  if (!_interval(first) || !_interval(second)) return false;
  final a = _map(first);
  final b = _map(second);
  return (a['startUs'] as int) < (b['endUs'] as int) &&
      (b['startUs'] as int) < (a['endUs'] as int);
}

bool _indexSearches(Object? value, Object? indexedPath) {
  if (value is! List || value.length != _indexVerificationPages.length) {
    return false;
  }
  for (var i = 0; i < _indexVerificationPages.length; i++) {
    final page = _indexVerificationPages[i];
    final search = _map(value[i]);
    final hits = search['hits'];
    if (search['page'] is! int ||
        search['page'] != page ||
        search['query'] != 'PDF preview fixture - page $page' ||
        search['totalHits'] is! int ||
        search['matchingHits'] is! int ||
        search['matchingHits'] != 1 ||
        hits is! List ||
        search['totalHits'] != hits.length) {
      return false;
    }
    final own = hits
        .map(_map)
        .where(
          (hit) =>
              hit['filePath'] == indexedPath &&
              hit['isPdf'] == true &&
              hit['segment'] == (page - 1).toString(),
        );
    if (own.length != 1) return false;
  }
  return true;
}

bool _raster(Object? value, String format, {bool premultipliedAlpha = false}) {
  final data = _map(value);
  return _positive(data['width']) &&
      _positive(data['height']) &&
      data['pixelFormat'] == format &&
      data['alphaPremultiplied'] == premultipliedAlpha &&
      data['pixelBytes'] is int &&
      data['pixelBytes'] ==
          (data['width'] as int) * (data['height'] as int) * 4 &&
      data['inkPixels'] is int &&
      (data['inkPixels'] as int) > 10 &&
      (data['inkPixels'] as int) <=
          (data['width'] as int) * (data['height'] as int) &&
      _hash(data['sha256']);
}

bool _closed(Object? value, Object? opened) {
  final data = _map(value);
  final views = data['liveViewIdsAfter'];
  return _integer(data['viewId']) &&
      _positive(data['nativeHandle']) &&
      _sameReader(data, opened) &&
      _allOwnersClosed(data['ownerLifetimes']) &&
      _ownerWasClosed(data['ownerLifetimes'], opened) &&
      data['documentEventsClosed'] == true &&
      data['nativeDisposeAwaited'] == true &&
      data['registryEntryRemoved'] == true &&
      _ids(views) &&
      !(views as List).contains(data['viewId']);
}

/// בודק ראיות של החלונות שהתרחיש יצר; אינו ממציא ערכים כאשר ראיה חסרה.
List<String> perfPdfMultiWindowErrors(Object? value) {
  final report = _map(value);
  final errors = <String>[];
  void require(bool condition, String description) {
    if (!condition) errors.add(description);
  }

  require(
    report['schemaVersion'] is int &&
        report['schemaVersion'] == 1 &&
        report['status'] == 'passed',
    'Multi-window report did not complete successfully.',
  );
  require(
    const ['windows', 'linux', 'macos'].contains(report['platform']) &&
        report['bindingMode'] == 'framework' &&
        report['realPlatformDispatcher'] == true &&
        report['windowingOwnerInstalled'] == true &&
        _positive(report['engineId']),
    'Real desktop dispatcher and windowing owner are required.',
  );
  final initialViews = report['initialViewIds'];
  require(_ids(initialViews), 'Initial native view IDs are missing.');
  final fixture = _map(report['fixture']);
  final source = fixture['sourcePath'];
  final copy = fixture['copyPath'];
  require(
    fixture['sourceKind'] == 'generated' &&
        fixture['pageCount'] is int &&
        (fixture['pageCount'] as int) >= 4 &&
        _path(source) &&
        _path(copy) &&
        source != copy &&
        _hash(fixture['sha256']) &&
        fixture['copySha256'] == fixture['sha256'],
    'Two independent paths to the same verified generated PDF are required.',
  );

  final steps = _map(report['steps']);
  for (final name in perfPdfMultiWindowSteps) {
    require(
      _map(steps[name])['status'] == 'passed',
      'Missing or failed multi-window step: $name',
    );
  }
  final open = _map(steps['open']);
  final primary = open['primary'];
  final secondary = open['secondary'];
  require(
    _reader(primary, 1, source) &&
        _reader(secondary, 2, copy) &&
        _distinctReaders(primary, secondary) &&
        open['separateDocuments'] == true &&
        open['separateControllers'] == true,
    'Opening did not prove independent readers in distinct native views.',
  );
  if (_ids(initialViews)) {
    require(
      (initialViews as List).contains(_map(primary)['viewId']) &&
          !initialViews.contains(_map(secondary)['viewId']),
      'The primary must be an initial view and the secondary must be new.',
    );
  }

  final concurrent = _map(steps['concurrentWork']);
  final operations = _map(concurrent['submittedOperations']);
  require(
    _overlap(operations['viewing'], operations['indexing']) &&
        _overlap(operations['viewing'], operations['printing']),
    'Viewing must overlap submitted indexing and printing operations.',
  );
  require(
    _reader(concurrent['primary'], 3, source) &&
        _sameReader(concurrent['primary'], primary) &&
        _reader(concurrent['secondary'], 4, copy) &&
        _sameReader(concurrent['secondary'], secondary) &&
        _ownerOperation(concurrent['primaryDuringSecondaryNavigation']) &&
        _ownerOperation(concurrent['secondaryDuringPrimaryNavigation']) &&
        concurrent['primaryUnchangedBySecondaryNavigation'] == true &&
        concurrent['secondaryUnchangedByPrimaryNavigation'] == true,
    'Navigation did not preserve both reader identities and independent targets.',
  );
  final indexing = _map(concurrent['indexing']);
  require(
    indexing['clean'] == true &&
        indexing['indexedBooks'] is int &&
        indexing['indexedBooks'] == 1 &&
        indexing['indexedPath'] == source &&
        _pages(indexing['verifiedPages'], _indexVerificationPages) &&
        _indexSearches(indexing['searchResults'], source),
    'The actual index did not return the expected PDF pages.',
  );
  final printing = _map(concurrent['printing']);
  require(
    _pages(printing['sourcePages'], const [2, 3]) &&
        printing['textPreserved'] == true &&
        printing['rightToLeft'] == true &&
        printing['width'] == 480 &&
        printing['height'] == 320 &&
        _hash(printing['sha256']) &&
        _hash(printing['pngSha256']) &&
        printing['pngBytes'] is int &&
        (printing['pngBytes'] as int) > 8 &&
        _raster(
          printing['raster'],
          'rgba8888',
          premultipliedAlpha: report['platform'] == 'macos',
        ) &&
        _map(printing['raster'])['width'] == 480 &&
        _map(printing['raster'])['height'] == 320,
    'Vector output, RTL order and real printing raster proof are required.',
  );

  for (final name in const [
    'minimizeRestore',
    'closeSecondary',
    'closeReopened',
  ]) {
    final step = _map(steps[name]);
    require(
      _raster(step['raster'], 'bgra8888') &&
          _overlap(step['rasterInterval'], step['transitionInterval']),
      'Native raster work did not overlap the transition: $name',
    );
    require(
      _reader(step['primaryAfter'], 3, source) &&
          _sameReader(step['primaryAfter'], primary) &&
          step['primaryUnchanged'] == true,
      'Primary reader identity or image changed after: $name',
    );
  }
  final minimize = _map(steps['minimizeRestore']);
  require(
    minimize['nativeMinimized'] == true &&
        _reader(minimize['secondaryAfter'], 4, copy) &&
        _sameReader(minimize['secondaryAfter'], secondary),
    'Native minimize and restore did not retain the secondary reader.',
  );
  require(
    _closed(_map(steps['closeSecondary'])['closed'], secondary),
    'First secondary view or native PDF document was not fully closed.',
  );

  final reopened = _map(steps['reopen']);
  final reopenedReader = reopened['secondary'];
  require(
    _reader(reopenedReader, 2, copy) &&
        _distinctReaders(primary, reopenedReader) &&
        reopened['differentDocument'] == true,
    'Reopening did not produce an independent document and native view.',
  );
  if (_ids(initialViews)) {
    require(
      !(initialViews as List).contains(_map(reopenedReader)['viewId']),
      'A reopened secondary must not impersonate an initial view.',
    );
  }
  require(
    _closed(_map(steps['closeReopened'])['closed'], reopenedReader),
    'Reopened secondary view or native PDF document was not fully closed.',
  );

  final teardown = _map(steps['teardown']);
  final finalViews = teardown['finalViewIds'];
  require(
    teardown['primaryDocumentEventsClosed'] == true &&
        teardown['primaryNativeDisposeAwaited'] == true &&
        _allOwnersClosed(teardown['primaryOwnerLifetimes']) &&
        teardown['ownedWindowsRemaining'] is int &&
        teardown['ownedWindowsRemaining'] == 0 &&
        teardown['ownedDocumentsRemaining'] is int &&
        teardown['ownedDocumentsRemaining'] == 0 &&
        teardown['pendingOperations'] is int &&
        teardown['pendingOperations'] == 0 &&
        teardown['indexingActive'] == false &&
        _ids(finalViews) &&
        _ids(initialViews) &&
        (finalViews as List).length == (initialViews as List).length &&
        finalViews.toSet().containsAll(initialViews),
    'Owned PDF work or native views remain after teardown.',
  );
  final primaryLifetimes = teardown['primaryOwnerLifetimes'];
  final secondaryLifetimes = _map(
    _map(steps['closeSecondary'])['closed'],
  )['ownerLifetimes'];
  final reopenedLifetimes = _map(
    _map(steps['closeReopened'])['closed'],
  )['ownerLifetimes'];
  for (final value in [
    primary,
    concurrent['primary'],
    concurrent['primaryDuringSecondaryNavigation'],
    for (final name in [
      'minimizeRestore',
      'closeSecondary',
      'closeReopened',
    ]) ...[
      _map(steps[name])['primaryAfter'],
      _map(steps[name])['primaryDuringTransition'],
    ],
  ]) {
    require(
      _ownerOperation(value) && _ownerWasClosed(primaryLifetimes, value),
      'A primary operation used an unverified owner or omitted its native close.',
    );
  }
  for (final value in [
    secondary,
    concurrent['secondary'],
    concurrent['secondaryDuringPrimaryNavigation'],
    _map(steps['minimizeRestore'])['secondaryAfter'],
  ]) {
    require(
      _ownerOperation(value) && _ownerWasClosed(secondaryLifetimes, value),
      'A secondary operation used an unverified owner or omitted its native close.',
    );
  }
  require(
    _ownerWasClosed(reopenedLifetimes, reopenedReader),
    'The reopened reader owner has no matching native close receipt.',
  );
  return errors;
}
