import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pdfrx/pdfrx.dart';

import '../../integration_test/support/pdf_native_owner_tracker.dart';

class _Document extends Fake implements PdfDocument {
  _Document(this.sourceName, {this.closeGate, this.textGate}) {
    pages = [_Page(this)];
  }

  @override
  final String sourceName;
  final Completer<void>? closeGate;
  final Completer<void>? textGate;
  final eventStream = StreamController<PdfDocumentEvent>.broadcast();
  final eventsDelivered = Completer<void>();
  Future<void>? _disposing;
  bool closing = false;
  bool nativeClosed = false;
  int reads = 0;

  @override
  late List<PdfPage> pages;

  @override
  Stream<PdfDocumentEvent> get events => eventStream.stream;

  @override
  Future<void> dispose() => _disposing ??= () async {
    closing = true;
    await eventStream.close();
    eventsDelivered.complete();
    if (closeGate != null) await closeGate!.future;
    nativeClosed = true;
  }();
}

class _Page extends Fake implements PdfPage {
  _Page(this.document);

  @override
  final _Document document;

  @override
  Future<PdfPageRawText?> loadText() async {
    document.reads++;
    if (document.closing) return null;
    if (document.textGate != null) await document.textGate!.future;
    return PdfPageRawText('bundleruntime bundlepage1', []);
  }
}

class _Controller extends PdfViewerController {
  _Controller(_Document document) : _document = document {
    _reference = PdfDocumentRefDirect(document);
    _listenable = _reference.resolveListenable();
    _releaseHost = _listenable.addListener(() {});
    _listenable.setDocument(document);
  }

  PdfDocument _document;
  late final PdfDocumentRef _reference;
  late final PdfDocumentListenable _listenable;
  late final VoidCallback _releaseHost;
  final _listeners = <VoidCallback>[];
  final _visible = ValueNotifier(PdfVisibleRenderState.ready);
  bool _ready = true;

  @override
  bool get isReady => _ready;

  @override
  PdfDocument get document => _document;

  @override
  PdfDocumentRef get documentRef => _reference;

  @override
  ValueListenable<PdfVisibleRenderState> get visibleRenderState => _visible;

  @override
  void addListener(VoidCallback listener) => _listeners.add(listener);

  @override
  void removeListener(VoidCallback listener) => _listeners.remove(listener);

  void replace(_Document document, {bool updateController = true}) {
    if (updateController) _document = document;
    _listenable.setDocument(document);
    for (final listener in List<VoidCallback>.of(_listeners)) {
      listener();
    }
  }

  void detach() {
    _ready = false;
    _visible.value = PdfVisibleRenderState.detached;
    _releaseHost();
  }

  void finish() => _visible.dispose();
}

Future<void> _close(
  _Controller controller,
  PdfNativeOwnerTracker owners,
) async {
  controller.detach();
  owners.stopObserving();
  await owners.waitForNativeClose(const Duration(seconds: 1));
  controller.finish();
}

void main() {
  test(
    'replacement resolves the live owner instead of the pre-ready snapshot',
    () async {
      final old = _Document('file%/replacement.pdf');
      final next = _Document('file%/replacement.pdf');
      final controller = _Controller(old);
      final owners = PdfNativeOwnerTracker(controller);
      final initial = owners.capture('initial ready');
      controller.replace(next);
      await old.eventsDelivered.future;

      // הבקרה השלילית משחזרת את ה־handle שנשמר לפני המתנת הניווט.
      expect(await initial.document.pages.first.loadText(), isNull);
      final current = owners.capture('text after navigation');
      expect(current.document, same(next));
      expect(current.generation, 2);
      expect(
        (await current.document.pages.first.loadText())?.fullText,
        contains('bundlepage1'),
      );
      owners.verify(current, 'text after navigation');
      expect(() => owners.verify(initial, 'stale owner'), throwsStateError);
      expect(owners.receipt['replacementsObserved'], 1);
      await _close(controller, owners);
      expect(old.nativeClosed && next.nativeClosed, true);
    },
  );

  test(
    'replacement during a native await fails the unchanged-owner guard',
    () async {
      final gate = Completer<void>();
      final old = _Document('file%/native-await.pdf', textGate: gate);
      final next = _Document('file%/native-await.pdf');
      final controller = _Controller(old);
      final owners = PdfNativeOwnerTracker(controller);
      final before = owners.capture('native text');
      final pending = before.document.pages.first.loadText();
      controller.replace(next);
      gate.complete();
      expect((await pending)?.fullText, contains('bundlepage1'));
      expect(() => owners.verify(before, 'native text'), throwsStateError);
      final checks = owners.receipt['checks']! as List<Map<String, Object?>>;
      expect(checks.last['ownerUnchangedDuringOperation'], false);
      await _close(controller, owners);
    },
  );

  test(
    'close waits for every observed native owner without retaining the ref',
    () async {
      final oldGate = Completer<void>();
      final nextGate = Completer<void>();
      final old = _Document('file%/native-close.pdf', closeGate: oldGate);
      final next = _Document('file%/native-close.pdf', closeGate: nextGate);
      final controller = _Controller(old);
      final owners = PdfNativeOwnerTracker(controller);
      controller.replace(next);
      controller.detach();
      expect(
        next.closing,
        true,
        reason: 'The tracker must not retain a documentRef listener.',
      );
      owners.stopObserving();
      await Future.wait([
        old.eventsDelivered.future,
        next.eventsDelivered.future,
      ]);
      var finished = false;
      final closing = owners
          .waitForNativeClose(const Duration(seconds: 1))
          .then((_) => finished = true);
      oldGate.complete();
      await old.dispose();
      expect(finished, false);
      expect(owners.allNativeClosesCompleted, false);
      nextGate.complete();
      await closing;
      expect(owners.allNativeClosesCompleted, true);
      expect(owners.receipt['allNativeClosesCompleted'], true);
      final receipts = owners.receipt['owners']! as List<Map<String, Object?>>;
      expect(receipts, hasLength(2));
      expect(
        receipts.every(
          (owner) =>
              owner['documentEventsClosed'] == true &&
              owner['nativeDisposeAwaited'] == true,
        ),
        true,
      );
      controller.finish();
    },
  );

  test(
    'a controller holding the previous published owner is rejected',
    () async {
      final old = _Document('file%/published.pdf');
      final next = _Document('file%/published.pdf');
      final controller = _Controller(old);
      final owners = PdfNativeOwnerTracker(controller);
      controller.replace(next, updateController: false);
      expect(() => owners.capture('stale controller'), throwsStateError);
      controller.replace(next);
      owners.capture('published controller');
      await _close(controller, owners);
    },
  );

  test(
    'another PDF source is not accepted as a legal recovery generation',
    () async {
      final old = _Document('file%/expected.pdf');
      final foreign = _Document('file%/foreign.pdf');
      final controller = _Controller(old);
      final owners = PdfNativeOwnerTracker(controller);
      controller.replace(foreign);
      expect(() => owners.capture('foreign document'), throwsStateError);
      final checks = owners.receipt['checks']! as List<Map<String, Object?>>;
      expect(checks.last['matchesSourceName'], false);
      await _close(controller, owners);
    },
  );

  test(
    'a snapshot from another controller cannot establish identity',
    () async {
      final first = _Controller(_Document('file%/first.pdf'));
      final second = _Controller(_Document('file%/second.pdf'));
      final firstOwners = PdfNativeOwnerTracker(first);
      final secondOwners = PdfNativeOwnerTracker(second);
      final snapshot = firstOwners.capture('first window');
      expect(
        () => secondOwners.verify(snapshot, 'second window'),
        throwsStateError,
      );
      await _close(first, firstOwners);
      await _close(second, secondOwners);
    },
  );
}
