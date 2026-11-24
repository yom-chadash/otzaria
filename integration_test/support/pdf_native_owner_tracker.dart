import 'dart:async';

import 'package:pdfrx/pdfrx.dart';

/// עוקב אחר מסמכי controller בלי להחזיק את ה־documentRef בחיים.
class PdfNativeOwnerTracker {
  PdfNativeOwnerTracker(this.controller)
    : _referenceKey = controller.documentRef.key,
      _sourceName = controller.document.sourceName {
    receipt['owners'] = _ownerReceipts;
    receipt['checks'] = _checks;
    receipt['expectedSourceName'] = _sourceName;
    controller.addListener(_observeController);
    controller.visibleRenderState.addListener(_observeController);
    _observeController();
  }

  final PdfViewerController controller;
  final PdfDocumentRefKey _referenceKey;
  final String _sourceName;
  final _owners = <_ObservedOwner>[];
  final _ownerReceipts = <Map<String, Object?>>[];
  final _checks = <Map<String, Object?>>[];
  bool _observing = true;

  final Map<String, Object?> receipt = {
    'scope': 'Controller owners observed through public notifications and operation boundaries; not every native open.',
    'retainsDocumentRef': false,
    'observerStopped': false,
    'allNativeClosesCompleted': false,
  };

  void _observeController() {
    if (_observing && controller.isReady) {
      _observe(controller.document);
    }
  }

  _ObservedOwner _observe(PdfDocument document) {
    for (final owner in _owners) {
      if (identical(owner.document, document)) return owner;
    }
    final owner = _ObservedOwner(document, _owners.length + 1);
    _owners.add(owner);
    _ownerReceipts.add(owner.receipt);
    receipt['ownersObserved'] = _owners.length;
    receipt['replacementsObserved'] = _owners.length - 1;
    return owner;
  }

  PdfNativeOwnerSnapshot capture(String operation) {
    if (!_observing || !controller.isReady) {
      throw StateError('PDF controller is not ready for $operation.');
    }
    final document = controller.document;
    final owner = _observe(document);
    final matchesReference = controller.documentRef.key == _referenceKey;
    final matchesSource = document.sourceName == _sourceName;
    final published = controller.documentRef.resolveListenable().document;
    final current =
        matchesReference &&
        matchesSource &&
        identical(document, published) &&
        !owner.closed.isCompleted;
    _checks.add({
      'operation': operation,
      'generation': owner.generation,
      'matchesReferenceKey': matchesReference,
      'matchesSourceName': matchesSource,
      'matchesPublishedOwner': identical(document, published),
      'closedEventObserved': owner.closed.isCompleted,
      'currentOwner': current,
    });
    if (!current) {
      throw StateError('PDF owner is stale for $operation.');
    }
    return PdfNativeOwnerSnapshot._(this, owner);
  }

  void verify(PdfNativeOwnerSnapshot before, String operation) {
    if (!identical(before._tracker, this)) {
      throw StateError('PDF owner belongs to another controller.');
    }
    final after = capture('$operation:after');
    final unchanged = identical(before.document, after.document);
    _checks.add({
      'operation': operation,
      'beforeGeneration': before.generation,
      'afterGeneration': after.generation,
      'ownerUnchangedDuringOperation': unchanged,
    });
    if (!unchanged) {
      throw StateError('PDF owner changed during $operation.');
    }
  }

  void stopObserving() {
    if (!_observing) return;
    _observeController();
    _observing = false;
    controller.removeListener(_observeController);
    controller.visibleRenderState.removeListener(_observeController);
    receipt['observerStopped'] = true;
  }

  Future<void> waitForNativeClose(Duration timeout) async {
    if (_observing) {
      throw StateError(
        'Stop observing the detached controller before close verification.',
      );
    }
    await Future.wait(
      _owners.map((owner) => owner.waitForNativeClose(timeout)),
    );
    receipt['allNativeClosesCompleted'] = allNativeClosesCompleted;
  }

  bool get allNativeClosesCompleted =>
      _owners.isNotEmpty &&
      _owners.every((owner) => owner.nativeCloseCompleted);
}

class PdfNativeOwnerSnapshot {
  PdfNativeOwnerSnapshot._(this._tracker, this._owner);

  final PdfNativeOwnerTracker _tracker;
  final _ObservedOwner _owner;

  PdfDocument get document => _owner.document;
  int get generation => _owner.generation;
}

class _ObservedOwner {
  _ObservedOwner(this.document, this.generation) {
    receipt.addAll({
      'generation': generation,
      'sourceName': document.sourceName,
      'documentIdentity': identityHashCode(document),
      'documentEventsClosed': false,
      'nativeDisposeAwaited': false,
    });
    events = document.events.listen(
      (_) {},
      onDone: () {
        receipt['documentEventsClosed'] = true;
        closed.complete();
      },
    );
  }

  final PdfDocument document;
  final int generation;
  final receipt = <String, Object?>{};
  final closed = Completer<void>();
  late final StreamSubscription<PdfDocumentEvent> events;
  bool nativeCloseCompleted = false;

  Future<void> waitForNativeClose(Duration timeout) async {
    try {
      // onDone מסמן התחלת סגירה; dispose החוזר ממתין גם לסיום native.
      await closed.future.timeout(timeout);
      await document.dispose().timeout(timeout);
      nativeCloseCompleted = true;
      receipt['nativeDisposeAwaited'] = true;
    } finally {
      await events.cancel();
    }
  }
}
