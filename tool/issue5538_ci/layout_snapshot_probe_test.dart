// Fork-only debug/JIT work counts and native correctness; no latency claims.
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:ui' as ui;

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:otzaria/pdf_book/utils/pdf_book_layout.dart';
import 'package:pdfrx/pdfrx.dart';

import 'layout_snapshot_metrics.dart';

const _source = String.fromEnvironment('M02_SOURCE');
const _appHash = String.fromEnvironment('M02_APP_HASH');
const _candidate = bool.fromEnvironment('M02_CANDIDATE');

String _hash(Object value) =>
    sha256.convert(utf8.encode(jsonEncode(value))).toString();

void _save(File path, Object value) {
  final pending = File('${path.path}.pending');
  pending.writeAsStringSync('${jsonEncode(value)}\n', flush: true);
  pending.renameSync(path.path);
}

PdfPageLayout _appLayout(
  BookViewLayoutMemo memo,
  List<PdfPage> pages,
  PdfDocument? owner,
  bool cover,
  double margin,
) {
  if (_candidate) {
    final dynamic candidateMemo = memo;
    return candidateMemo.layoutNativeDocumentPages(
      pages,
      document: owner,
      hasCover: cover,
      verticalMargin: margin,
    ) as PdfPageLayout;
  }
  return memo.layout(pages, hasCover: cover, verticalMargin: margin);
}

class _CountPage extends Fake implements PdfPage {
  _CountPage(this.w, this.h);
  double w;
  double h;
  int reads = 0;
  @override
  double get width {
    reads++;
    return w;
  }

  @override
  double get height {
    reads++;
    return h;
  }
}

class _MetadataOwner extends Fake implements PdfDocument {
  _MetadataOwner(this.pages);
  @override
  List<PdfPage> pages;
}

Future<void> _pumpReady(
  WidgetTester tester,
  PdfViewerController controller, {
  bool visible = false,
}) async {
  for (var i = 0; i < 2000; i++) {
    await tester.pump(const Duration(milliseconds: 10));
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 5)),
    );
    if (controller.isReady &&
        (!visible ||
            controller.visibleRenderState.value ==
                PdfVisibleRenderState.ready)) {
      await tester.pump();
      return;
    }
  }
  throw StateError('Native viewer did not become ready.');
}

Future<void> _finishNativeDisposal(WidgetTester tester, PdfDocument document) async {
  var completed = false;
  final disposing = document.dispose().then((_) => completed = true);
  for (var i = 0; i < 100 && !completed; i++) {
    await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 10)));
    await tester.pump(const Duration(milliseconds: 10));
  }
  expect(completed, isTrue, reason: 'native disposal must finish before leaving fake time');
  await disposing;
}

Map<String, Object?> _layoutProof(PdfViewerController controller) {
  final layout = controller.layout;
  final rects = [
    for (final r in layout.pageLayouts) [r.left, r.top, r.right, r.bottom],
  ];
  final hits = [
    for (final r in layout.pageLayouts)
      controller
          .getPdfPageHitTestResult(r.center, useDocumentLayoutCoordinates: true)
          ?.page
          .pageNumber,
  ];
  expect(
    hits,
    orderedEquals(List.generate(rects.length, (index) => index + 1)),
  );
  return {
    'pageCount': rects.length,
    'documentSize': [layout.documentSize.width, layout.documentSize.height],
    'allRectanglesSha256': _hash(rects),
    'allCenterHitsSha256': _hash(hits),
  };
}

Future<Map<String, Object?>> _pageProof(PdfPage page) async {
  final text = await page.loadText();
  final image = await page.render(
    width: 128,
    height: 160,
    fullWidth: 128,
    fullHeight: 160,
    backgroundColor: 0xffffffff,
  );
  if (image == null) throw StateError('Native page raster is missing.');
  try {
    final value = text?.fullText ?? '';
    final rectangles =
        text?.charRects
            .map((r) => [r.left, r.top, r.right, r.bottom])
            .toList() ??
        [];
    return {
      'page': page.pageNumber,
      'size': [page.width, page.height],
      'rotation': page.rotation.index,
      'text': value,
      'utf16Units': value.length,
      'textSha256': sha256.convert(utf8.encode(value)).toString(),
      'charRectCount': rectangles.length,
      'allCharRectanglesSha256': _hash(rectangles),
      'rasterWidth': image.width,
      'rasterHeight': image.height,
      'rasterSha256': sha256.convert(image.pixels).toString(),
    };
  } finally {
    image.dispose();
  }
}

Future<Map<String, Object?>> _viewportProof(
  WidgetTester tester,
  PdfViewerController controller,
  GlobalKey boundaryKey,
  Directory output,
  String name,
) async {
  await _pumpReady(tester, controller, visible: true);
  await tester.pump();
  final owner = controller.document;
  final page = controller.pageNumber;
  final result = await tester.runAsync(() async {
    final boundary =
        boundaryKey.currentContext!.findRenderObject()!
            as RenderRepaintBoundary;
    final image = await boundary.toImage(pixelRatio: 1);
    try {
      final rgba = (await image.toByteData(
        format: ui.ImageByteFormat.rawRgba,
      ))!;
      final png = (await image.toByteData(format: ui.ImageByteFormat.png))!;
      final path = '$name.png';
      await File('${output.path}/$path').writeAsBytes(png.buffer.asUint8List());
      return <String, Object?>{
        'pngPath': path,
        'pngSha256': sha256.convert(png.buffer.asUint8List()).toString(),
        'rgbaSha256': sha256.convert(rgba.buffer.asUint8List()).toString(),
        'width': image.width,
        'height': image.height,
        'page': page,
      };
    } finally {
      image.dispose();
    }
  });
  expect(
    controller.isReady &&
        identical(controller.document, owner) &&
        controller.pageNumber == page,
    isTrue,
  );
  expect(controller.visibleRenderState.value, PdfVisibleRenderState.ready);
  return result!;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final config = jsonDecode(
    File(Platform.environment['M02_PROBE_CONFIG']!).readAsStringSync(),
  ) as Map<String, dynamic>;
  final output = Directory(config['output'] as String);
  final inputs = (config['documents'] as List).cast<Map<String, dynamic>>();
  final rows = <Map<String, Object?>>[];
  final parity = <String, Object?>{};
  final completedTests = <String>[];
  void checkpoint(String status) {
    _save(File('${output.path}/result.json'), {
      'status': status,
      'pid': pid,
      'source': _source,
      'appHash': _appHash,
      'candidate': _candidate,
      'executionMode': 'flutter-test-debug-jit',
      'instrumented': true,
      'latencyClaim': false,
      'counts': rows,
      'completedTests': completedTests,
      'parity': parity,
      'scope': 'Synthetic metadata helper counts; actual PdfViewer on synthetic native documents and real PDFs. Full initial native page text/geometry/raster hashes; affected first/last pages after mutations; full layout and center-hit parity. Not PdfBookScreen or OS presentation.',
    });
  }

  tearDown(() => checkpoint('partial'));

  setUpAll(() async {
    expect(kDebugMode && Platform.isLinux, isTrue);
    expect(_source, config['source']);
    expect(_appHash, config['appHash']);
    expect(_candidate, config['candidate']);
    expect(Issue5538LayoutCounters.marker, 'm02-layout-counts-v1');
    for (final item in inputs) {
      expect(
        sha256
            .convert(await File(item['path'] as String).readAsBytes())
            .toString(),
        item['sha256'],
      );
    }
    Pdfrx.pdfiumModulePath = config['pdfium'] as String;
    Pdfrx.cacheDirectoryPath = '${output.path}/native-cache';
    Directory(Pdfrx.cacheDirectoryPath!).createSync(recursive: true);
    Pdfrx.normalizeHebrewText = true;
    Pdfrx.decodeLegacyHebrewText = true;
    await pdfrxInitialize();
    final forceNative = await PdfDocument.openFile(
      inputs.first['path'] as String,
    );
    await forceNative.dispose();
    _save(File('${output.path}/ready.json'), {
      'pid': pid,
      'source': _source,
      'appHash': _appHash,
      'candidate': _candidate,
      'packageConfig': (await Isolate.packageConfig)?.toString(),
      'executionMode': 'flutter-test-debug-jit',
      'instrumented': true,
    });
    final watch = Stopwatch()..start();
    while (!File('${output.path}/start').existsSync()) {
      if (watch.elapsed > const Duration(seconds: 60)) {
        throw StateError('Host identity handshake missing.');
      }
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }
  });

  for (final count in [100, 1000, 10000]) {
    test('synthetic metadata getter work $count', () {
      final pages = List<_CountPage>.generate(
        count,
        (i) => _CountPage(400.0 + i % 3, 600.0 + i % 5),
      );
      final owner = _MetadataOwner(List<PdfPage>.unmodifiable(pages));
      final memo = BookViewLayoutMemo();
      final first = _appLayout(memo, owner.pages, owner, true, 16);
      final firstReads = pages.fold(0, (sum, page) => sum + page.reads);
      expect(firstReads, count * 2);
      for (final page in pages) {
        page.reads = 0;
      }
      for (var i = 0; i < 200; i++) {
        expect(
          identical(_appLayout(memo, owner.pages, owner, true, 16), first),
          isTrue,
        );
      }
      final hitReads = pages.fold(0, (sum, page) => sum + page.reads);
      expect(hitReads, _candidate ? 0 : 400 * count);
      for (final page in pages) {
        page.reads = 0;
      }
      final changed = _appLayout(memo, owner.pages, owner, false, 16);
      expect(pages.fold(0, (sum, page) => sum + page.reads), count * 2);
      expect(changed, isNot(same(first)));
      rows.add({
        'kind': 'synthetic-metadata-helper',
        'pages': count,
        'cacheHits': 200,
        'firstBuildSizeGetters': firstReads,
        'cacheHitSizeGetters': hitReads,
        'parameterChangeSizeGetters': count * 2,
      });
      completedTests.add('synthetic metadata getter work $count');
    });
  }

  test('general mutable page metadata still invalidates', () {
    final page = _CountPage(400, 600);
    final memo = BookViewLayoutMemo();
    final first = memo.layout([page], hasCover: true, verticalMargin: 16);
    page.h = 900;
    final next = memo.layout([page], hasCover: true, verticalMargin: 16);
    expect(next, isNot(same(first)));
    expect(next.pageLayouts.single.height, 900);
    parity['generalMutablePageSemantics'] = true;
    completedTests.add('general mutable page metadata still invalidates');
  });

  for (final item in inputs) {
    testWidgets('native layout ${item['id']}', (tester) async {
      tester.view.physicalSize = const Size(400, 500);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final document = (await tester.runAsync(
        () => PdfDocument.openFile(item['path'] as String),
      ))!;
      expect(document.pages.length, item['pageCount']);
      final isReal = item['kind'] == 'real-pdf';
      final controller = PdfViewerController();
      final boundary = GlobalKey();
      final memo = BookViewLayoutMemo();
      var cover = true;
      var margin = 16.0;
      var calls = 0;
      PdfPageLayout? mutableOverride;
      Widget viewer() => MaterialApp(
        home: RepaintBoundary(
          key: boundary,
          child: PdfViewer(
            PdfDocumentRefDirect(document, autoDispose: false),
            controller: controller,
            params: PdfViewerParams(
              isVisible: isReal,
              margin: margin / 2,
              backgroundColor: Colors.white,
              onDocumentChanged: (_) => memo.clear(),
              layoutPages: (pages, _) {
                calls++;
                if (mutableOverride != null) return mutableOverride!;
                final owner =
                    !kIsWeb &&
                        PdfrxEntryFunctions.instance.backendType ==
                            PdfrxBackendType.pdfium &&
                        controller.isReady
                    ? controller.document
                    : null;
                return _appLayout(memo, pages, owner, cover, margin);
              },
            ),
          ),
        ),
      );
      try {
        await tester.pumpWidget(viewer());
        await _pumpReady(tester, controller, visible: isReal);
        controller.value = controller.value.clone()
          ..translateByDouble(0, -1, 0, 1);
        await _pumpReady(tester, controller, visible: isReal);
        final stablePages = document.pages;
        final beforeLayout = _layoutProof(controller);
        for (final phase in ['pan', 'zoom']) {
          Issue5538LayoutCounters.reset();
          final callsBefore = calls;
          var matrixChanges = 0;
          for (var step = 0; step < 100; step++) {
            final before = controller.value.storage.toList();
            if (phase == 'pan') {
              controller.value = controller.value.clone()
                ..translateByDouble(0, step.isEven ? -1 : 1, 0, 1);
            } else {
              final factor = step.isEven ? 1.01 : 1 / 1.01;
              controller.value = controller.value.clone()
                ..scaleByDouble(factor, factor, 1, 1);
            }
            await tester.pump(const Duration(milliseconds: 1));
            if (!listEquals(before, controller.value.storage)) matrixChanges++;
          }
          final counters = Issue5538LayoutCounters.snapshot();
          expect(identical(document.pages, stablePages), isTrue);
          expect(matrixChanges, greaterThan(0));
          expect(calls - callsBefore, greaterThan(0));
          expect(counters['indexBuilds'], 0);
          expect(
            counters['rectChecks'],
            _candidate ? 0 : counters['matchCalls']! * document.pages.length,
          );
          if (_candidate) expect(counters['matchCalls'], 0);
          if (!_candidate) expect(counters['matchCalls'], greaterThan(0));
          expect(_layoutProof(controller), beforeLayout);
          rows.add({
            'kind': 'native-viewer-stable-matrix',
            'phase': phase,
            'id': item['id'],
            'pages': document.pages.length,
            'sourceKind': item['kind'],
            'matrixUpdates': 100,
            'matrixChanges': matrixChanges,
            'layoutCallbacks': calls - callsBefore,
            'viewerVisible': isReal,
            'counts': counters,
          });
        }
        final states = <String, Object?>{'initial': beforeLayout};
        if (isReal) {
          states['initialViewport'] = await _viewportProof(
            tester,
            controller,
            boundary,
            output,
            '${item['id']}-initial',
          );
          states['allInitialPages'] = await tester.runAsync(
            () async => [
              for (final page in document.pages) await _pageProof(page),
            ],
          );
        }
        final originals = document.pages;
        for (final operation in [
          'rotate-first',
          'reverse',
          'remove-tail',
          'parameters',
        ]) {
          if (operation == 'rotate-first') {
            document.pages = [
              originals.first.rotatedCW90(),
              ...originals.skip(1),
            ];
          } else if (operation == 'reverse') {
            document.pages = document.pages.reversed.toList();
          } else if (operation == 'remove-tail') {
            document.pages = document.pages
                .take(document.pages.length - 1)
                .toList();
          } else {
            cover = false;
            margin = 8;
          }
          final immediate = _appLayout(
            memo,
            document.pages,
            document,
            cover,
            margin,
          );
          final expected = buildBookViewPageLayout(
            pageSizes: [
              for (final page in document.pages) Size(page.width, page.height),
            ],
            hasCover: cover,
            verticalMargin: margin,
          );
          expect(immediate, expected);
          await tester.pumpWidget(viewer());
          controller.invalidate();
          await _pumpReady(tester, controller, visible: isReal);
          expect(controller.layout, expected);
          states[operation] = _layoutProof(controller);
          if (isReal) {
            states['$operation-affectedPages'] = await tester.runAsync(
              () async => [
                await _pageProof(document.pages.first),
                await _pageProof(document.pages.last),
              ],
            );
          }
        }
        final rects = List<Rect>.of(controller.layout.pageLayouts);
        mutableOverride = PdfPageLayout(
          pageLayouts: rects,
          documentSize: controller.layout.documentSize,
        );
        controller.invalidate();
        await _pumpReady(tester, controller, visible: isReal);
        rects[0] = rects[0].translate(30, 0);
        controller.value = controller.value.clone()
          ..translateByDouble(0, -1, 0, 1);
        await _pumpReady(tester, controller, visible: isReal);
        expect(controller.layout.pageLayouts.first, rects.first);
        expect(
          controller
              .getPdfPageHitTestResult(
                rects.first.center,
                useDocumentLayoutCoordinates: true,
              )
              ?.page
              .pageNumber,
          1,
        );
        states['mutableRectListDetected'] = true;
        parity[item['id'] as String] = states;
      } finally {
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
        memo.clear();
        await _finishNativeDisposal(tester, document);
      }
      completedTests.add('native layout ${item['id']}');
    }, timeout: const Timeout(Duration(minutes: 12)));
  }

  tearDownAll(() async {
    checkpoint(
      completedTests.length == 4 + inputs.length ? 'completed' : 'partial',
    );
    _save(File('${output.path}/completion.json'), {
      'pid': pid,
      'source': _source,
      'appHash': _appHash,
    });
    final watch = Stopwatch()..start();
    while (!File('${output.path}/stop').existsSync()) {
      if (watch.elapsed > const Duration(seconds: 60)) {
        throw StateError('Host completion identity handshake missing.');
      }
      await Future<void>.delayed(const Duration(milliseconds: 10));
    }
  });
}
