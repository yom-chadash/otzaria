// Fork-only S02 experiment. The application receives native documents directly.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:otzaria/pdf_book/utils/pdf_font_fallback.dart';
import 'package:otzaria/theme/app_fonts.dart';
import 'package:pdfrx/pdfrx.dart';

const _source = String.fromEnvironment('PDF_5538_FONT_SOURCE');
const _appSources = String.fromEnvironment('PDF_5538_FONT_APP_SOURCES');

Future<void> _save(File file, Object? value) async {
  final pending = File('${file.path}.pending');
  await pending.writeAsString('${jsonEncode(value)}\n', flush: true);
  await pending.rename(file.path);
}

Future<String> _fileHash(File file) async =>
    (await sha256.bind(file.openRead()).first).toString();

Future<void> _until(bool Function() predicate, String description) async {
  final deadline = DateTime.now().add(const Duration(seconds: 45));
  while (!predicate()) {
    if (DateTime.now().isAfter(deadline)) {
      throw StateError('Timed out: $description');
    }
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  if (!Platform.isLinux || !kProfileMode || _source.length != 40) {
    throw StateError('S02 requires the pinned Linux profile build.');
  }
  final config = jsonDecode(
    await File(Platform.environment['PDF_5538_FONT_CONFIG']!).readAsString(),
  ) as Map<String, dynamic>;
  final output = Directory(config['output'] as String);
  final errors = <String>[];
  FlutterError.onError = (details) {
    errors.add(details.toString());
    FlutterError.presentError(details);
  };
  ui.PlatformDispatcher.instance.onError = (error, stack) {
    errors.add('$error\n$stack');
    return true;
  };
  try {
    if (config['sourceCommit'] != _source ||
        config['appSourcesSha256'] != _appSources ||
        await _fileHash(File(config['pdfium'] as String)) !=
            config['pdfiumSha256']) {
      throw StateError('Compiled source or PDFium identity differs.');
    }
    final documents = (config['documents'] as List)
        .cast<Map<String, dynamic>>();
    for (final input in documents) {
      final file = File(input['absolutePath'] as String);
      if (await _fileHash(file) != input['sha256'] ||
          await file.length() != input['sizeBytes']) {
        throw StateError('Fixture identity differs: ${input['id']}');
      }
    }
    Pdfrx.pdfiumModulePath = config['pdfium'] as String;
    Pdfrx.cacheDirectoryPath =
        (await Directory('${output.path}/native-cache').create()).path;
    Pdfrx.normalizeHebrewText = true;
    Pdfrx.decodeLegacyHebrewText = true;
    await pdfrxFlutterInitialize();
    runApp(const MaterialApp(home: ColoredBox(color: Colors.white)));
    await WidgetsBinding.instance.endOfFrame;
    await _save(File('${output.path}/ready.json'), {
      'pid': pid,
      'sourceCommit': _source,
      'appSourcesSha256': _appSources,
      'mode': 'profile',
    });
    await _until(() => File('${output.path}/start').existsSync(), 'host receipt');
    final result = await _measure(config, output, documents);
    result['frameworkErrors'] = errors;
    await _save(File('${output.path}/result.json'), result);
    exit(0);
  } catch (error, stack) {
    await _save(File('${output.path}/failure.json'), {
      'error': error.toString(),
      'stack': stack.toString(),
      'frameworkErrors': errors,
    });
    exit(1);
  }
}

Future<Map<String, Object?>> _measure(
  Map<String, dynamic> config,
  Directory output,
  List<Map<String, dynamic>> documents,
) async {
  final scenario = config['scenario'] as String;
  final route = config['route'] as String;
  if (route != 'app' && route != 'builtin') {
    throw StateError('Unknown font integration route: $route');
  }
  final builtin = route == 'builtin';
  final builtinManager = builtin
      ? PdfFontManager.platform(resolvers: [
          const _BuiltinSystemFamilyResolver(),
          const _BuiltinBundledFontResolver(),
        ])
      : null;
  final original = PdfrxEntryFunctions.instance;
  final probe = _Observer(original);
  PdfrxEntryFunctions.instance = probe;
  final view = GlobalKey<_ProbeViewState>();
  final slots = <_Slot>[
    for (final input in documents)
      _Slot(input, _documentRef(input, builtin), builtinManager),
  ];
  final frames = _Frames(output, probe, slots);
  final finalProofs = <String, Object?>{};
  final referenceProofs = <String, Object?>{};
  final referenceFrames = <String, String>{};
  final referenceFrameRows = <Map<String, Object?>>[];
  var lateDiscoveryObserved = false;
  final gated = scenario == 'close-pending-font' ||
      scenario == 'replace-pending-font' ||
      scenario == 'shared-fonts' ||
      scenario == 'different-fonts';
  if (gated) probe.registrationGate = Completer<void>();
  try {
    frames.start();
    runApp(_ProbeView(key: view, slots: slots, probe: probe));
    await WidgetsBinding.instance.endOfFrame;
    if (gated) {
      await _until(() => probe.fontStarts > 0, 'a real font registration');
      if (scenario == 'close-pending-font') {
        slots.first.visible = false;
        view.currentState!.update();
        await WidgetsBinding.instance.endOfFrame;
        probe.record('closeRequested', {'inputId': slots.first.input['id']});
      } else if (scenario == 'replace-pending-font') {
        final source = slots.first;
        final replacement = documents.last;
        // Keep the same PdfViewer key/controller while changing its source.
        source
          ..input = replacement
          ..ref = _documentRef(replacement, builtin);
        slots.last.visible = false;
        view.currentState!.update();
        await WidgetsBinding.instance.endOfFrame;
        probe.record('replaceRequested', {'inputId': replacement['id']});
      } else {
        await _until(
          () => documents.every((input) => probe.documents.any(
            (document) => document.sourceName == input['absolutePath'],
          )),
          'every concurrent native open',
        );
      }
      // Deterministic lifecycle pressure, separately labelled from natural
      // first-good timing. It changes no font policy or PDF/native bytes.
      await Future<void>.delayed(const Duration(milliseconds: 150));
      probe.registrationGate!.complete();
      probe.record('registrationGateReleased', {});
    }
    // Observe native/font quiescence. Calling listenable.load again here can
    // enqueue another open when a still-loading viewer is closed, which would
    // manufacture a baseline lifecycle failure in the measurement itself.
    await _settle(probe, slots);
    if (scenario == 'late-page' || scenario == 'late-text') {
      final slot = slots.first;
      final before = probe.missingEvents;
      probe.record('lateDiscoveryRequested', {'kind': scenario});
      if (scenario == 'late-text') {
        final document = slot.controller.document;
        final page = slot.input['targetPage'] as int;
        // Page measurement is explicit and is included in this scenario. The
        // emitted native event records identify where discovery actually fell.
        await document.reloadPages(pageNumbersToReload: [page]);
        if (slot.controller.isReady) {
          await slot.controller.document.pages[page - 1].loadText();
        }
      }
      if (slot.controller.isReady) {
        await slot.controller.goToPage(
          pageNumber: slot.input['targetPage'] as int,
          duration: Duration.zero,
        );
      }
      await _settle(probe, slots);
      lateDiscoveryObserved = probe.missingEvents > before;
      // A refresh can reset initialPageNumber; explicitly return to the target
      // for the parity observation and record this as harness navigation.
      await slot.controller.goToPage(
        pageNumber: slot.input['targetPage'] as int,
        duration: Duration.zero,
      );
      await _settle(probe, slots);
    }
    for (final slot in slots.where((slot) => slot.visible)) {
      if (!slot.controller.isReady) continue;
      final document = slot.controller.document;
      final proof = await _proof(
        document,
        slot.input['targetPage'] as int,
      );
      finalProofs[slot.input['id'] as String] = {
        ...proof,
        'stableDocument': slot.controller.isReady &&
            identical(slot.controller.document, document),
      };
    }
    await frames.stop();
    final measuredEvents = probe.events.toList();
    final measuredFrames = frames.rows.toList();
    final measuredSkips = frames.skipped;

    // End the measured app flow before isolated references register any font
    // that an old baseline missed. Native documents are never wrapped.
    for (final slot in slots) {
      slot.visible = false;
    }
    view.currentState!.update();
    await WidgetsBinding.instance.endOfFrame;
    await Future<void>.delayed(Duration.zero);
    await probe.disposeDocuments();
    probe.phase = 'reference';

    final activeIds = finalProofs.keys.toSet();
    for (final input in documents.where((input) => activeIds.contains(input['id']))) {
      final document = await PdfFontFallback.openFile(input['absolutePath'] as String);
      referenceProofs[input['id'] as String] = await _proof(
        document,
        input['targetPage'] as int,
      );
      final slot = slots.first;
      slot
        ..input = input
        ..ref = PdfDocumentRefDirect(document, autoDispose: false)
        ..initialPage = input['targetPage'] as int
        ..visible = true;
      view.currentState!.update();
      await _settle(probe, slots);
      await slot.controller.goToPage(
        pageNumber: input['targetPage'] as int,
        duration: Duration.zero,
      );
      await _settle(probe, slots);
      final row = await frames.captureSlot(slot, phase: 'reference');
      referenceFrameRows.add(row);
      referenceFrames[input['id'] as String] = row['rgbaSha256'] as String;
      slot.visible = false;
      view.currentState!.update();
      await WidgetsBinding.instance.endOfFrame;
      await document.dispose();
    }
    runApp(const SizedBox.shrink());
    await WidgetsBinding.instance.endOfFrame;
    await probe.disposeDocuments();
    await _save(File('${output.path}/frame-receipts.json'), {
      'measured': measuredFrames,
      'reference': referenceFrameRows,
    });

    final parity = <String, Object?>{};
    for (final entry in finalProofs.entries) {
      final actual = entry.value! as Map;
      final reference = referenceProofs[entry.key]! as Map;
      final input = documents.singleWhere((input) => input['id'] == entry.key);
      final word = input['expectedWord'] as String;
      final samples = measuredFrames.where((frame) =>
        frame['inputId'] == entry.key &&
        frame['pageNumber'] == input['targetPage'] &&
        frame['stableDocument'] == true,
      ).toList();
      final good = samples.where((frame) =>
        frame['renderState'] == 'ready' &&
        frame['rgbaSha256'] == referenceFrames[entry.key],
      ).toList();
      final firstGood = good.isEmpty ? null : good.first['elapsedUs'] as int;
      parity[entry.key] = {
        'textEqual': actual['textSha256'] == reference['textSha256'],
        'geometryEqual': actual['geometrySha256'] == reference['geometrySha256'],
        'fullPageRasterEqual': actual['rasterSha256'] == reference['rasterSha256'],
        'stableMeasuredDocument': actual['stableDocument'],
        'referenceContainsExpectedWord': (reference['text'] as String).contains(word),
        'referenceHasInk': word.isEmpty || (reference['inkPixels'] as int) > 100,
        'firstGoodSampleUs': firstGood,
        'sampledReadyFramesBeforeRecovery': samples.where((frame) =>
          frame['renderState'] == 'ready' &&
          (firstGood == null || (frame['elapsedUs'] as int) < firstGood) &&
          frame['rgbaSha256'] != referenceFrames[entry.key],
        ).length,
        'sampledMismatchingReadyFrames': samples.where((frame) =>
          frame['renderState'] == 'ready' &&
          frame['rgbaSha256'] != referenceFrames[entry.key],
        ).length,
        'sampledPendingFrames': samples.where((frame) => frame['renderState'] == 'pending').length,
        'samples': samples.length,
      };
    }
    return {
      'schemaVersion': 1,
      'status': 'completed',
      'pid': pid,
      'mode': 'profile',
      'sourceCommit': _source,
      'appSourcesSha256': _appSources,
      'variant': config['variant'],
      'route': route,
      'scenario': scenario,
      'injectedRegistrationHoldMs': gated ? 150 : 0,
      'lateDiscoveryObserved': lateDiscoveryObserved,
      'measuredEvents': measuredEvents,
      'measuredFrames': measuredFrames,
      'captureBusyFramesSkipped': measuredSkips,
      'frameCaptureErrors': frames.errors,
      'finalProofs': finalProofs,
      'referenceProofs': referenceProofs,
      'referenceFrames': referenceFrames,
      'parity': parity,
      'allEvents': probe.events,
      'allNativeCloseFuturesCompleted': true,
      'limitations': [
        'Synthetic variable-width unembedded-font fixtures; actual native query charset/pitch is recorded.',
        'The real PdfFontFallback.documentRef and PdfViewer are used; no PdfDocument/PdfPage wrappers.',
        'Reference uses isolated fresh app open after the measured flow with unchanged resolver policy, not an independent PDF ground truth.',
        'First-good requires ready state and a matching sampled layer-tree readback, not a placeholder or OS scanout; captures can miss frames.',
        'Only mismatching ready frames prove a completed page image differed; pending mismatches can be normal placeholders.',
        'Lifecycle/shared cases contain an explicit registration gate; do not claim natural speedups from those timings.',
        'late-text includes explicit target-page measurement; native event receipts delimit what was actually discovered.',
        'The builtin route uses PdfViewer.fontManager with the identical copied resolver policy, checked against application source by the host.',
      ],
    };
  } finally {
    if (probe.registrationGate case final gate?) {
      if (!gate.isCompleted) gate.complete();
    }
    await frames.stop();
    runApp(const SizedBox.shrink());
    await WidgetsBinding.instance.endOfFrame;
    await probe.disposeDocuments();
    PdfrxEntryFunctions.instance = original;
  }
}

PdfDocumentRef _documentRef(Map<String, dynamic> input, bool builtin) => builtin
    ? PdfDocumentRefFile(input['absolutePath'] as String)
    : PdfFontFallback.documentRef(input['absolutePath'] as String);

Future<void> _settle(_Observer probe, List<_Slot> slots) async {
  final deadline = DateTime.now().add(const Duration(seconds: 45));
  await _until(
    () => probe.pending == 0 && slots.where((slot) => slot.visible).every(
      (slot) => slot.controller.isReady &&
          slot.controller.visibleRenderState.value == PdfVisibleRenderState.ready,
    ),
    'visible page images',
  );
  // Allow queued font completions/reloads to become visible before accepting
  // this state. This is a harness quiet window, never application policy.
  while (true) {
    if (DateTime.now().isAfter(deadline)) {
      throw StateError('The measured viewer never reached a quiet ready state.');
    }
    final revision = probe.events.length;
    await Future<void>.delayed(const Duration(milliseconds: 250));
    if (revision == probe.events.length && probe.pending == 0 &&
        slots.where((slot) => slot.visible).every((slot) =>
          slot.controller.isReady &&
          slot.controller.visibleRenderState.value == PdfVisibleRenderState.ready,
        )) return;
    await _until(() => probe.pending == 0, 'pending native/font operations');
  }
}

Future<Map<String, Object?>> _proof(PdfDocument document, int pageNumber) async {
  if (!document.pages[pageNumber - 1].isLoaded) {
    await document.reloadPages(pageNumbersToReload: [pageNumber]);
  }
  final page = document.pages[pageNumber - 1];
  final text = await page.loadText();
  final image = await page.render(
    fullWidth: 320,
    fullHeight: 400,
    width: 320,
    height: 400,
    backgroundColor: 0xffffffff,
  );
  if (image == null) throw StateError('Full-page native raster failed.');
  try {
    var ink = 0;
    for (var i = 0; i < image.pixels.length; i += 4) {
      if (image.pixels[i] < 128 && image.pixels[i + 1] < 128 && image.pixels[i + 2] < 128) ink++;
    }
    final fullText = text?.fullText ?? '';
    final geometry = text?.charRects.map((rect) => [rect.left, rect.top, rect.right, rect.bottom]).toList() ?? [];
    return {
      'text': fullText,
      'textSha256': sha256.convert(utf8.encode(fullText)).toString(),
      'utf16CodeUnits': fullText.length,
      'geometrySha256': sha256.convert(utf8.encode(jsonEncode(geometry))).toString(),
      'charRects': geometry.length,
      'rasterSha256': sha256.convert(image.pixels).toString(),
      'rasterWidth': image.width,
      'rasterHeight': image.height,
      'inkPixels': ink,
    };
  } finally {
    image.dispose();
  }
}

class _Slot {
  _Slot(this.input, this.ref, this.fontManager);
  Map<String, dynamic> input;
  PdfDocumentRef ref;
  final PdfFontManager? fontManager;
  final controller = PdfViewerController();
  final boundary = GlobalKey();
  final viewer = GlobalKey();
  int initialPage = 1;
  bool visible = true;
}

class _ProbeView extends StatefulWidget {
  const _ProbeView({super.key, required this.slots, required this.probe});
  final List<_Slot> slots;
  final _Observer probe;
  @override
  State<_ProbeView> createState() => _ProbeViewState();
}

class _ProbeViewState extends State<_ProbeView> {
  void update() => setState(() {});

  @override
  Widget build(BuildContext context) => MaterialApp(
    home: ColoredBox(
      color: Colors.white,
      child: Center(
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (final slot in widget.slots)
              SizedBox(
                width: 320,
                height: 400,
                child: slot.visible
                    ? RepaintBoundary(
                        key: slot.boundary,
                        child: PdfViewer(
                          slot.ref,
                          key: slot.viewer,
                          controller: slot.controller,
                          fontManager: slot.fontManager,
                          initialPageNumber: slot.initialPage,
                          params: PdfViewerParams(
                            margin: 0,
                            backgroundColor: Colors.white,
                            pageDropShadow: null,
                            horizontalCacheExtent: 0,
                            verticalCacheExtent: 0,
                            behaviorControlParams: const PdfViewerBehaviorControlParams(
                              loadPageDimensionsOnDemand: true,
                            ),
                            onDocumentChanged: (document) => widget.probe.record(
                              'viewerDocument',
                              {'inputId': slot.input['id'], 'documentId': widget.probe.id(document)},
                            ),
                          ),
                        ),
                      )
                    : const ColoredBox(color: Colors.white),
              ),
          ],
        ),
      ),
    ),
  );
}

class _Frames {
  _Frames(this.output, this.probe, this.slots);
  final Directory output;
  final _Observer probe;
  final List<_Slot> slots;
  final rows = <Map<String, Object?>>[];
  final errors = <String>[];
  bool active = false;
  bool busy = false;
  int skipped = 0;
  int serial = 0;

  void start() {
    active = true;
    WidgetsBinding.instance.addPersistentFrameCallback((_) {
      if (!active) return;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!active) return;
        if (busy) {
          skipped++;
          return;
        }
        unawaited(_capture());
      });
    });
  }

  Future<void> _capture() async {
    busy = true;
    try {
      for (final slot in slots.where((slot) => slot.visible)) {
        if (slot.boundary.currentContext == null) continue;
        rows.add(await captureSlot(slot, phase: 'measurement'));
      }
    } catch (error, stack) {
      errors.add('$error\n$stack');
    } finally {
      busy = false;
    }
  }

  Future<Map<String, Object?>> captureSlot(_Slot slot, {required String phase}) async {
    final inputId = slot.input['id'];
    final document = slot.controller.isReady ? slot.controller.document : null;
    final page = slot.controller.isReady ? slot.controller.pageNumber : null;
    final state = slot.controller.visibleRenderState.value.name;
    final elapsed = probe.clock.elapsedMicroseconds;
    final boundary = slot.boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary;
    final image = await boundary.toImage(pixelRatio: 1);
    try {
      final rgba = (await image.toByteData(format: ui.ImageByteFormat.rawRgba))!;
      final png = (await image.toByteData(format: ui.ImageByteFormat.png))!;
      final path = 'frame-${serial++}.png';
      await File('${output.path}/$path').writeAsBytes(png.buffer.asUint8List(), flush: true);
      return {
        'phase': phase,
        'inputId': inputId,
        'documentId': probe.id(document),
        'pageNumber': page,
        'renderState': state,
        'elapsedUs': elapsed,
        'stableDocument': document != null && slot.controller.isReady &&
            identical(slot.controller.document, document) &&
            slot.controller.pageNumber == page && slot.input['id'] == inputId,
        'width': image.width,
        'height': image.height,
        'rgbaSha256': sha256.convert(rgba.buffer.asUint8List()).toString(),
        'pngSha256': sha256.convert(png.buffer.asUint8List()).toString(),
        'pngPath': path,
      };
    } finally {
      image.dispose();
    }
  }

  Future<void> stop() async {
    active = false;
    await _until(() => !busy, 'frame capture completion');
  }
}

class _Observer implements PdfrxEntryFunctions {
  _Observer(this.base);
  final PdfrxEntryFunctions base;
  final clock = Stopwatch()..start();
  final documents = <PdfDocument>[];
  final events = <Map<String, Object?>>[];
  Completer<void>? registrationGate;
  String phase = 'measurement';
  int pending = 0;
  int fontStarts = 0;
  int missingEvents = 0;

  int? id(PdfDocument? document) {
    if (document == null) return null;
    final index = documents.indexOf(document);
    return index < 0 ? null : index;
  }

  void record(String type, Map<String, Object?> data) {
    events.add({'type': type, 'phase': phase, 'elapsedUs': clock.elapsedMicroseconds, ...data});
  }

  Future<void> disposeDocuments() async {
    var disposed = 0;
    final deadline = DateTime.now().add(const Duration(seconds: 45));
    while (true) {
      // A late completion may append another actual native document while
      // disposal is awaited; use an index instead of a fail-fast iterator.
      while (disposed < documents.length) {
        await documents[disposed++].dispose();
      }
      final revision = events.length;
      await Future<void>.delayed(const Duration(milliseconds: 250));
      if (pending == 0 && disposed == documents.length && revision == events.length) return;
      if (DateTime.now().isAfter(deadline)) {
        throw StateError('Native/font cleanup did not become quiet.');
      }
    }
  }

  @override
  Future<PdfDocument> openFile(String path, {
    PdfPasswordProvider? passwordProvider,
    bool firstAttemptByEmptyPassword = true,
    bool useProgressiveLoading = false,
  }) async {
    pending++;
    final start = clock.elapsedMicroseconds;
    try {
      final document = await base.openFile(path,
        passwordProvider: passwordProvider,
        firstAttemptByEmptyPassword: firstAttemptByEmptyPassword,
        useProgressiveLoading: useProgressiveLoading,
      );
      final documentId = documents.length;
      documents.add(document);
      record('nativeOpen', {'documentId': documentId, 'path': path, 'startedUs': start, 'pageCount': document.pages.length});
      document.events.listen((event) {
        if (event is! PdfDocumentMissingFontsEvent) return;
        missingEvents++;
        Object? byPage;
        try {
          // Optional public field added by the narrow engine backport. A is
          // deliberately compiled against the old API without this field.
          final dynamic withPages = event;
          final map = withPages.missingFontsByPage as Map;
          byPage = {for (final entry in map.entries) '${entry.key}': (entry.value as List).map((query) => _query(query as PdfFontQuery)).toList()};
        } on NoSuchMethodError {
          byPage = null;
        }
        record('missingFonts', {
          'documentId': documentId,
          'eventOwnsDocument': identical(event.document, document),
          'queries': event.missingFonts.map(_query).toList(),
          'byPage': byPage,
        });
      }, onDone: () => record('documentEventsClosed', {'documentId': documentId}));
      return document;
    } finally {
      pending--;
    }
  }

  static Map<String, Object?> _query(PdfFontQuery query) => {
    'face': query.face, 'weight': query.weight, 'italic': query.isItalic,
    'charset': query.charset.pdfiumCharsetId, 'pitchFamily': query.pitchFamily,
  };

  @override
  Future<void> addFontData({required String face, required Uint8List data, String? resolvedFace}) async {
    pending++;
    fontStarts++;
    record('fontRegistrationStart', {'face': face, 'resolvedFace': resolvedFace, 'bytes': data.length, 'fontSha256': sha256.convert(data).toString(), 'kind': 'data'});
    try {
      await registrationGate?.future;
      await base.addFontData(face: face, data: data, resolvedFace: resolvedFace);
      record('fontRegistered', {'face': face});
    } finally { pending--; }
  }

  @override
  Future<void> addFontFile({required String face, required String filePath, String? resolvedFace}) async {
    pending++;
    fontStarts++;
    try {
      record('fontRegistrationStart', {'face': face, 'resolvedFace': resolvedFace, 'fontSha256': await _fileHash(File(filePath)), 'kind': 'file'});
      await registrationGate?.future;
      await base.addFontFile(face: face, filePath: filePath, resolvedFace: resolvedFace);
      record('fontRegistered', {'face': face});
    } finally { pending--; }
  }

  @override
  Future<void> reloadFonts() async {
    pending++;
    try {
      await base.reloadFonts();
      record('backendFontReload', {});
    } finally { pending--; }
  }

  @override
  Future<void> init() => base.init();
  @override
  Future<T> suspendPdfiumWorkerDuringAction<T>(FutureOr<T> Function() action) => base.suspendPdfiumWorkerDuringAction(action);
  @override
  Future<R> compute<M, R>(FutureOr<R> Function(M) callback, M message) => base.compute(callback, message);
  @override
  Future<void> stopBackgroundWorker() => base.stopBackgroundWorker();
  @override
  PdfrxBackendType get backendType => base.backendType;
  @override
  Future<PdfDocument> openAsset(String name, {PdfPasswordProvider? passwordProvider, bool firstAttemptByEmptyPassword = true, bool useProgressiveLoading = false}) => base.openAsset(name, passwordProvider: passwordProvider, firstAttemptByEmptyPassword: firstAttemptByEmptyPassword, useProgressiveLoading: useProgressiveLoading);
  @override
  Future<PdfDocument> openData(Uint8List data, {PdfPasswordProvider? passwordProvider, bool firstAttemptByEmptyPassword = true, String? sourceName, bool allowDataOwnershipTransfer = false, bool useProgressiveLoading = false, int? maxSizeToCacheOnMemory, void Function()? onDispose}) => base.openData(data, passwordProvider: passwordProvider, firstAttemptByEmptyPassword: firstAttemptByEmptyPassword, sourceName: sourceName, allowDataOwnershipTransfer: allowDataOwnershipTransfer, useProgressiveLoading: useProgressiveLoading, maxSizeToCacheOnMemory: maxSizeToCacheOnMemory, onDispose: onDispose);
  @override
  Future<PdfDocument> openCustom({required FutureOr<int> Function(Uint8List, int, int) read, required int fileSize, required String sourceName, PdfPasswordProvider? passwordProvider, bool firstAttemptByEmptyPassword = true, bool useProgressiveLoading = false, int? maxSizeToCacheOnMemory, void Function()? onDispose}) => base.openCustom(read: read, fileSize: fileSize, sourceName: sourceName, passwordProvider: passwordProvider, firstAttemptByEmptyPassword: firstAttemptByEmptyPassword, useProgressiveLoading: useProgressiveLoading, maxSizeToCacheOnMemory: maxSizeToCacheOnMemory, onDispose: onDispose);
  @override
  Future<PdfDocument> openUri(Uri uri, {PdfPasswordProvider? passwordProvider, bool firstAttemptByEmptyPassword = true, bool useProgressiveLoading = false, PdfDownloadProgressCallback? progressCallback, bool preferRangeAccess = false, Map<String, String>? headers, bool withCredentials = false, Duration? timeout}) => base.openUri(uri, passwordProvider: passwordProvider, firstAttemptByEmptyPassword: firstAttemptByEmptyPassword, useProgressiveLoading: useProgressiveLoading, progressCallback: progressCallback, preferRangeAccess: preferRangeAccess, headers: headers, withCredentials: withCredentials, timeout: timeout);
  @override
  Future<PdfDocument> createNew({required String sourceName}) => base.createNew(sourceName: sourceName);
  @override
  Future<PdfDocument> createFromJpegData(Uint8List jpegData, {required double width, required double height, required String sourceName}) => base.createFromJpegData(jpegData, width: width, height: height, sourceName: sourceName);
  @override
  Future<void> configureFontEnvironment({String? fontCachePath, List<String> fontPaths = const []}) => base.configureFontEnvironment(fontCachePath: fontCachePath, fontPaths: fontPaths);
  @override
  Future<void> clearAllFontData() => base.clearAllFontData();
}

// Byte-checked copy of the unchanged app policy for the builtin comparator.
class _BuiltinSystemFamilyResolver implements PdfFontResolver {
  const _BuiltinSystemFamilyResolver();

  @override
  Future<PdfFontResolution?> resolve(
    PdfFontQuery query,
    PdfFontResolveContext context,
  ) async {
    await AppFonts.warmUpSystemFontsCache();
    final faces = AppFonts.pluginSystemFamilyFacesByCompactName(
      PdfFontFallback.familyFromPdfFace(query.face),
    );
    if (faces == null) return null;
    final isBold =
        query.weight >= 600 || query.face.toLowerCase().contains('bold');
    return PdfFontResolution.localFontFile(
      fontFilePath: isBold
          ? faces.boldPath ?? faces.regularPath
          : faces.regularPath,
      targetFace: query.face,
      resolvedFace: faces.family,
    );
  }
}

/// מוצא אחרון, גם באנדרואיד שאין בו גופני Windows: כל גופן אמיתי עם עברית
/// מחזיר את האותיות, ו-Tinos מכסה גם לטינית.
class _BuiltinBundledFontResolver implements PdfFontResolver {
  const _BuiltinBundledFontResolver();

  @override
  PdfFontResolution resolve(
    PdfFontQuery query,
    PdfFontResolveContext context,
  ) {
    return PdfFontResolution(
      loadData: ({onProgress}) async {
        final data = await rootBundle.load(
          PdfFontFallback.bundledFallbackAsset,
        );
        return data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes);
      },
      targetFace: query.face,
      resolvedFace: 'Tinos',
    );
  }
}
