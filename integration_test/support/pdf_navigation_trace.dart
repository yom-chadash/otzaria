// Fork-only diagnostic observer. Its samples are never performance evidence.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:pdfrx/pdfrx.dart';

/// Records competing navigation futures without replacing or awaiting them.
class PdfNavigationTrace {
  PdfNavigationTrace({required this.observe, this.maximumRecords = 4096});

  final Map<String, Object?> Function() observe;
  final int maximumRecords;
  final List<Map<String, Object?>> records = [];
  final Map<int, String> pending = {};
  final Stopwatch _clock = Stopwatch()..start();
  int _call = 0;
  int droppedRecords = 0;
  bool _closed = false;

  void record(String event, [Map<String, Object?> fields = const {}]) {
    if (_closed) return;
    if (records.length >= maximumRecords) {
      droppedRecords++;
      return;
    }
    Map<String, Object?> state;
    try {
      state = observe();
    } catch (error) {
      state = {'observationError': '$error'};
    }
    records.add({
      'sequence': records.length,
      'elapsedUs': _clock.elapsedMicroseconds,
      'event': event,
      ...fields,
      'pendingCallIds': pending.keys.toList(growable: false),
      'state': state,
    });
  }

  Future<void> call(
    String method,
    Map<String, Object?> arguments,
    Future<void> Function() invoke,
  ) {
    final id = ++_call;
    pending[id] = method;
    record('navigation.call', {
      'callId': id,
      'method': method,
      'arguments': arguments,
      'stack': StackTrace.current.toString().split('\n').take(24).toList(),
    });
    try {
      final original = invoke();
      // A side observer only: return the exact original Future to its caller.
      // A superseded TickerFuture remains pending if that is the locked API's
      // behavior. No timeout, retry, cancellation, or destination is inserted.
      unawaited(original.then<void>((_) {
        pending.remove(id);
        record('navigation.completed', {'callId': id, 'method': method});
      }, onError: (Object error, StackTrace stack) {
        pending.remove(id);
        record('navigation.error', {
          'callId': id,
          'method': method,
          'error': '$error',
          'stack': '$stack',
        });
      }));
      return original;
    } catch (error, stack) {
      pending.remove(id);
      record('navigation.synchronous_error', {
        'callId': id,
        'method': method,
        'error': '$error',
        'stack': '$stack',
      });
      rethrow;
    }
  }

  Future<void> save(File file) async {
    record('trace.closed');
    _closed = true;
    if (await file.exists()) throw StateError('Navigation trace already exists');
    await file.writeAsString('${jsonEncode({
      'schemaVersion': 1,
      'diagnosticOnly': true,
      'performanceComparable': false,
      'futureSemantics': 'same original Future; completion side observer only',
      'clock': 'monotonic microseconds since trace construction',
      'droppedRecords': droppedRecords,
      'pendingCallsAtClose': pending.map((key, value) => MapEntry('$key', value)),
      'records': records,
    })}\n', flush: true);
  }
}

/// Public controller extension; captures engine size-delegate calls as well as
/// the application's navigation handler. Engine-private calls are identified
/// as a limitation rather than inferred from an absent record.
class TracedPdfViewerController extends PdfViewerController {
  TracedPdfViewerController({required this.applicationState}) {
    trace = PdfNavigationTrace(observe: _snapshot);
    addListener(_matrixChanged);
    visibleRenderState.addListener(_renderStateChanged);
  }

  final Map<String, Object?> Function() applicationState;
  late final PdfNavigationTrace trace;
  final List<PdfDocument> _documents = [];
  final List<StreamSubscription<PdfDocumentEvent>> _subscriptions = [];
  final Set<int> watchedPages = {1};

  static List<double> _rect(Rect value) => [
    value.left, value.top, value.width, value.height,
  ];

  Map<String, Object?> _snapshot() {
    final state = <String, Object?>{
      ...applicationState(),
      'controllerReady': isReady,
      'visibleRenderState': visibleRenderState.value.name,
    };
    if (!isReady) return state;
    final current = document;
    if (!_documents.any((value) => identical(value, current))) {
      _documents.add(current);
      _subscriptions.add(current.events.listen((event) {
        trace.record('document.event', {
          'eventType': event.type.name,
          'documentIdentity': identityHashCode(event.document),
          if (event is PdfDocumentPageStatusChangedEvent)
            'changedPageNumbers': event.changes.keys.toList(growable: false),
        });
      }));
    }
    state.addAll({
      'documentIdentity': identityHashCode(current),
      'documentEpoch': _documents.indexWhere((value) => identical(value, current)) + 1,
      'pageCount': pageCount,
      'loadedPageCount': current.pages.where((page) => page.isLoaded).length,
      'controllerPageNumber': pageNumber,
      'matrix': value.storage.toList(growable: false),
      'viewSize': [viewSize.width, viewSize.height],
      'visibleRect': _rect(visibleRect),
      'layoutIdentity': identityHashCode(layout),
      'layoutPageCount': layout.pageLayouts.length,
      'documentSize': [layout.documentSize.width, layout.documentSize.height],
      'watchedPages': {
        for (final number in watchedPages)
          if (number > 0 && number <= current.pages.length)
            '$number': {
              'loaded': current.pages[number - 1].isLoaded,
              'width': current.pages[number - 1].width,
              'height': current.pages[number - 1].height,
              if (number <= layout.pageLayouts.length)
                'rect': _rect(layout.pageLayouts[number - 1]),
            },
      },
    });
    return state;
  }

  void _matrixChanged() => trace.record('controller.matrix');
  void _renderStateChanged() => trace.record('controller.visible_render_state');

  @override
  Future<void> goTo(
    Matrix4? destination, {
    Duration duration = const Duration(milliseconds: 200),
  }) => trace.call('goTo', {
    'destination': destination?.storage.toList(growable: false),
    'durationUs': duration.inMicroseconds,
  }, () => super.goTo(destination, duration: duration));

  @override
  Future<void> goToPosition({
    required Offset documentOffset,
    double? zoom,
    Duration duration = Duration.zero,
  }) => trace.call('goToPosition', {
    'documentOffset': [documentOffset.dx, documentOffset.dy],
    'zoom': zoom,
    'durationUs': duration.inMicroseconds,
  }, () => super.goToPosition(
    documentOffset: documentOffset, zoom: zoom, duration: duration,
  ));

  @override
  Future<void> goToPage({
    required int pageNumber,
    PdfPageAnchor? anchor,
    Duration duration = const Duration(milliseconds: 200),
  }) => trace.call('goToPage', {
    'pageNumber': pageNumber,
    'anchor': anchor?.name,
    'durationUs': duration.inMicroseconds,
  }, () => super.goToPage(
    pageNumber: pageNumber, anchor: anchor, duration: duration,
  ));

  Future<void> saveAndStop(File file) async {
    trace.record('observer.stopping');
    removeListener(_matrixChanged);
    visibleRenderState.removeListener(_renderStateChanged);
    for (final subscription in _subscriptions) {
      await subscription.cancel();
    }
    await trace.save(file);
  }
}
