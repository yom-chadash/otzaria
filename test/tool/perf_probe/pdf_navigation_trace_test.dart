import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import '../../../integration_test/support/pdf_navigation_trace.dart';

void main() {
  test('diagnostic returns the identical Future and leaves an old call pending', () async {
    final trace = PdfNavigationTrace(observe: () => {'owner': 'same'});
    final old = Completer<void>();
    final newer = Completer<void>();
    final first = trace.call('goTo', {'target': 280}, () => old.future);
    final second = trace.call('goToPosition', {'target': 1}, () => newer.future);
    expect(identical(first, old.future), isTrue);
    expect(identical(second, newer.future), isTrue);
    newer.complete();
    await second;
    await Future<void>.value();
    expect(trace.pending, {1: 'goTo'});
    expect(trace.records.where((row) => row['event'] == 'navigation.completed').single['callId'], 2);
    old.complete();
    await first;
    await Future<void>.value();
    expect(trace.pending, isEmpty);
  });

  test('error observer does not turn the returned Future into success', () async {
    final trace = PdfNavigationTrace(observe: () => {});
    final operation = Completer<void>();
    final failure = StateError('original operation');
    final returned = trace.call('goTo', {}, () => operation.future);
    expect(identical(returned, operation.future), isTrue);
    final assertion = expectLater(returned, throwsA(same(failure)));
    operation.completeError(failure);
    await assertion;
    expect(trace.pending, isEmpty);
    expect(trace.records.last['event'], 'navigation.error');
  });

  test('synchronous failure and state observation failure preserve the operation', () {
    final trace = PdfNavigationTrace(observe: () => throw StateError('unattached layout'));
    final failure = StateError('original synchronous error');
    expect(() => trace.call('goTo', {}, () => throw failure), throwsA(same(failure)));
    expect(trace.pending, isEmpty);
    expect(trace.records.last['event'], 'navigation.synchronous_error');
    expect((trace.records.last['state'] as Map)['observationError'], contains('unattached layout'));
  });

  test('bounded records still update future state without dropping operations', () async {
    final trace = PdfNavigationTrace(observe: () => {}, maximumRecords: 1);
    final operation = Completer<void>();
    final returned = trace.call('goTo', {}, () => operation.future);
    operation.complete();
    await returned;
    await Future<void>.value();
    expect(trace.records, hasLength(1));
    expect(trace.droppedRecords, 1);
    expect(trace.pending, isEmpty);
  });
}
