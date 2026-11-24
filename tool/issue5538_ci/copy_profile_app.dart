// Fork-only real-viewer clipboard measurement for issue 5538.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:otzaria/pdf_book/utils/pdf_text_selection_actions.dart';
import 'package:pdfrx/pdfrx.dart';
import 'package:window_manager/window_manager.dart';

const _sourceCommit = String.fromEnvironment('PDF_5538_COPY_SOURCE_COMMIT');
const _selectionSource = String.fromEnvironment(
  'PDF_5538_COPY_SELECTION_SHA256',
);

Future<void> _json(File file, Object value) async {
  final pending = File('${file.path}.pending');
  await pending.writeAsString('${jsonEncode(value)}\n', flush: true);
  await pending.rename(file.path);
}

Future<String> _hash(File file) async =>
    (await sha256.bind(file.openRead()).first).toString();

Future<void> _phase(Directory output, String phase) =>
    _json(File('${output.path}/phase.json'), {
      'phase': phase,
      'epochUs': DateTime.now().microsecondsSinceEpoch,
    });

Future<void> _waitForFile(File file) async {
  final deadline = DateTime.now().add(const Duration(seconds: 90));
  while (!await file.exists()) {
    if (DateTime.now().isAfter(deadline)) {
      throw StateError('Host handshake timed out: ${file.path}');
    }
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}

Map<String, Object?> _rss() => {
  'epochUs': DateTime.now().microsecondsSinceEpoch,
  'rssBytes': ProcessInfo.currentRss,
  'processLifetimePeakBytes': ProcessInfo.maxRss,
};

Map<String, Object?> _snapshot(
  PdfPageTextCache cache,
  PdfViewerController controller,
  PdfTextSelectionActions actions,
) {
  final range = controller.isReady
      ? controller.textSelectionDelegate.textSelectionPointRange
      : null;
  return {
    ..._rss(),
    'cachePages': cache.length,
    'cacheEstimatedTextAndGeometryBytes': cache.estimatedBytes,
    'cacheMaximumPages': cache.maximumPages,
    'cacheMaximumBytes': cache.maximumBytes,
    'retainedTextSnapshotUtf16CodeUnits': actions.selectedTextSnapshot?.length,
    'selectionFirstPage': range?.start.text.pageNumber,
    'selectionLastPage': range?.end.text.pageNumber,
    'selectionBoundaryCharRects': range == null
        ? 0
        : range.start.text.charRects.length +
              (range.start.text.pageNumber == range.end.text.pageNumber
                  ? 0
                  : range.end.text.charRects.length),
  };
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  if (!Platform.isLinux || !kProfileMode || _sourceCommit.length != 40) {
    throw StateError('This fork-only clipboard probe requires Linux profile.');
  }
  final config =
      jsonDecode(
            await File(
              Platform.environment['PDF_5538_COPY_CONFIG']!,
            ).readAsString(),
          )
          as Map;
  final output = Directory(config['output'] as String);
  final errors = <String>[];
  FlutterError.onError = (details) {
    errors.add(details.toString());
    FlutterError.presentError(details);
  };
  try {
    final result = await _measure(config, output);
    await _phase(output, 'after_close');
    await Future<void>.delayed(const Duration(milliseconds: 250));
    result['afterClose'] = _rss();
    if (errors.isNotEmpty) throw StateError(errors.join('\n'));
    await _json(File('${output.path}/result.json'), result);
    exit(0);
  } catch (error, stack) {
    await _json(File('${output.path}/failure.json'), {
      'status': 'failed',
      'error': error.toString(),
      'stack': stack.toString(),
      'frameworkErrors': errors,
    });
    exit(1);
  }
}

Future<Map<String, Object?>> _measure(Map config, Directory output) async {
  final input = File(config['input'] as String);
  final pdfium = File(config['pdfium'] as String);
  if (config['sourceCommit'] != _sourceCommit ||
      config['selectionSourceSha256'] != _selectionSource ||
      await _hash(input) != config['inputSha256'] ||
      await _hash(pdfium) != config['pdfiumSha256']) {
    throw StateError('Compiled source or native/corpus identity differs.');
  }
  Pdfrx.pdfiumModulePath = await pdfium.resolveSymbolicLinks();
  Pdfrx.normalizeHebrewText = true;
  Pdfrx.decodeLegacyHebrewText = true;
  final nativeCache = await Directory('${output.path}/native-cache').create();
  await pdfrxInitialize(tmpPath: nativeCache.path);
  final beforeOpen = _rss();
  final openClock = Stopwatch()..start();
  final document = await PdfDocument.openFile(input.path);
  openClock.stop();
  final controller = PdfViewerController();
  var active = true;
  final actions = PdfTextSelectionActions(controller, isActive: () => active);
  final cache = PdfPageTextCache.forDocument(document);
  final snapshots = <String, Object?>{'beforeOpen': beforeOpen};
  final result = <String, Object?>{};
  try {
    if (document.pages.length != config['pageCount'] ||
        document.pages.any((page) => !page.isLoaded)) {
      throw StateError('The full pinned document is not loaded.');
    }
    final ready = Completer<void>();
    runApp(
      MaterialApp(
        home: Scaffold(
          body: Center(
            child: SizedBox(
              width: 960,
              height: 640,
              child: PdfViewer(
                PdfDocumentRefDirect(document, autoDispose: false),
                controller: controller,
                params: PdfViewerParams(
                  textSelectionParams: PdfTextSelectionParams(
                    onTextSelectionChange: (_) =>
                        actions.discardStaleSnapshot(),
                  ),
                  onViewerReady: (_, _) {
                    if (!ready.isCompleted) ready.complete();
                  },
                ),
              ),
            ),
          ),
        ),
      ),
    );
    await windowManager.ensureInitialized();
    await windowManager.setSize(const Size(1100, 820));
    await windowManager.show();
    await windowManager.focus();
    await ready.future.timeout(const Duration(minutes: 2));
    final renderDeadline = DateTime.now().add(const Duration(minutes: 2));
    while (controller.visibleRenderState.value != PdfVisibleRenderState.ready) {
      if (controller.visibleRenderState.value == PdfVisibleRenderState.failed ||
          DateTime.now().isAfter(renderDeadline)) {
        throw StateError('The real viewer did not paint its visible pages.');
      }
      await Future<void>.delayed(const Duration(milliseconds: 20));
    }
    await Clipboard.setData(const ClipboardData(text: ''));
    await _json(File('${output.path}/ready.json'), {
      'pid': pid,
      'mode': 'profile',
      'sourceCommit': _sourceCommit,
      'selectionSourceSha256': _selectionSource,
      'inputSha256': config['inputSha256'],
    });
    await _waitForFile(File('${output.path}/start'));
    snapshots['beforeSelectAll'] = _snapshot(cache, controller, actions);

    await _phase(output, 'select_all');
    final selectClock = Stopwatch()..start();
    final selected = await actions.selectAll();
    selectClock.stop();
    if (selected != PdfTextActionResult.none ||
        controller.textSelectionDelegate.textSelectionPointRange == null) {
      throw StateError('Production select-all did not create a selection.');
    }
    snapshots['afterSelectAll'] = _snapshot(cache, controller, actions);

    await _phase(output, 'copy');
    final copyClock = Stopwatch()..start();
    final copied = await actions.copy(keepSelection: true);
    copyClock.stop();
    snapshots['afterCopyBeforeValidation'] = _snapshot(
      cache,
      controller,
      actions,
    );
    if (copied != PdfTextActionResult.copied) {
      throw StateError('Production copy did not confirm the native write.');
    }

    await _phase(output, 'flutter_clipboard_readback');
    final readClock = Stopwatch()..start();
    final clipboard = await Clipboard.getData(Clipboard.kTextPlain);
    readClock.stop();
    snapshots['afterFlutterReadbackBeforeHash'] = _snapshot(
      cache,
      controller,
      actions,
    );
    await _phase(output, 'validation_hash');
    final text = actions.selectedTextSnapshot;
    if (text == null || text.isEmpty || clipboard?.text != text) {
      throw StateError('Native clipboard text differs from the app snapshot.');
    }
    final bytes = utf8.encode(text);
    final textIdentity = {
      'utf16CodeUnits': text.length,
      'utf8Bytes': bytes.length,
      'utf8Sha256': sha256.convert(bytes).toString(),
    };
    await _phase(output, 'independent_clipboard_readback');
    await _json(File('${output.path}/clipboard-ready.json'), {
      'pid': pid,
      'sourceCommit': _sourceCommit,
      ...textIdentity,
    });
    final externalFile = File('${output.path}/clipboard-readback.json');
    await _waitForFile(externalFile);
    final external = jsonDecode(await externalFile.readAsString()) as Map;
    if (external['status'] != 'passed' ||
        external['pid'] != pid ||
        external['sourceCommit'] != _sourceCommit ||
        external['utf8Sha256'] != textIdentity['utf8Sha256'] ||
        external['utf8Bytes'] != textIdentity['utf8Bytes']) {
      throw StateError('The independent X11 clipboard readback differs.');
    }
    result.addAll({
      'schemaVersion': 1,
      'status': 'passed',
      'mode': 'profile',
      'pid': pid,
      'sourceCommit': _sourceCommit,
      'selectionSourceSha256': _selectionSource,
      'documentId': config['documentId'],
      'inputSha256': config['inputSha256'],
      'pageCount': document.pages.length,
      'viewport': {
        'width': controller.viewSize.width,
        'height': controller.viewSize.height,
      },
      'flow':
          'real PdfViewer; app selectAll then keyboard-style copy keeping selection',
      'clipboardClearedBeforeMeasurement': true,
      'openUs': openClock.elapsedMicroseconds,
      'selectAllUs': selectClock.elapsedMicroseconds,
      'copyUs': copyClock.elapsedMicroseconds,
      'flutterClipboardReadbackUs': readClock.elapsedMicroseconds,
      'text': textIdentity,
      'actualNativeClipboardParity': true,
      'flutterClipboardParity': true,
      'snapshots': snapshots,
      'limitations': [
        'One current production flow; no product before/after or library-reference comparison.',
        'Copy time includes raw extraction, formatting, the full output string and the awaited native write.',
        'Clipboard readback and UTF-8 hashing allocate verification copies after the copy measurement.',
        'RSS includes native PDFium, Flutter, renderer, clipboard and allocator retention; it is not Dart allocation.',
        'Cache byte counts are the library estimate for retained text and geometry, not an exact heap size.',
        'Closing releases the app selection and document; native clipboard text stays owned until process exit.',
        'After-close RSS is sampled after 250 ms without forcing garbage collection.',
      ],
    });
  } finally {
    await _phase(output, 'close');
    final closeClock = Stopwatch()..start();
    active = false;
    actions.cancel();
    actions.releaseSelectedTextSnapshot();
    if (controller.isReady) {
      await controller.textSelectionDelegate.clearTextSelection();
    }
    runApp(const SizedBox.shrink());
    await WidgetsBinding.instance.endOfFrame;
    await document.dispose();
    await Future<void>.delayed(Duration.zero);
    closeClock.stop();
    result['closeUs'] = closeClock.elapsedMicroseconds;
    snapshots['documentDisposed'] = _snapshot(cache, controller, actions);
  }
  return result;
}
