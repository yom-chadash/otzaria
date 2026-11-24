// A test-only app for the real iOS Files picker. It uses the production service.
// Fork experiment: the large fixture is composed from a pinned real scanned PDF.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';
import 'package:otzaria/utils/file/mobile_pdf_export_service.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:otzaria/printing/pdf_sheet_composer.dart';
import 'package:pdfrx/pdfrx.dart';

void main() {
  if (!const bool.fromEnvironment('PDF_FILE_EXPORT_IOS_PROBE') ||
      !Platform.isIOS) {
    throw StateError(
      'This entrypoint is only for the iOS PDF export CI probe.',
    );
  }
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MaterialApp(home: _ExportProbe()));
}

/// Native lifetime tests run a real additional engine without a second app UI.
@pragma('vm:entry-point')
void pdfExportHeadlessProbe() {}

class _ExportProbe extends StatefulWidget {
  const _ExportProbe();

  @override
  State<_ExportProbe> createState() => _ExportProbeState();
}

class _ExportProbeState extends State<_ExportProbe> {
  MobilePdfExportOperation? _operation;
  File? _fixture;
  Map<String, Object?>? _composition;
  Directory? _destination;
  Directory? _evidence;
  String _state = 'preparing';
  String _summary = 'not_run';

  @override
  void initState() {
    super.initState();
    unawaited(_prepare());
  }

  Future<void> _prepare() async {
    try {
      final documents = await getApplicationDocumentsDirectory();
      final destination = Directory(
        p.join(documents.path, 'OtzariaExportUITest'),
      );
      await destination.create(recursive: true);
      // This makes the app's already-enabled Files location visible. The
      // directory is created only in this dedicated test entrypoint/build.
      await File(
        p.join(destination.path, 'fixture-location.txt'),
      ).writeAsString('Otzaria PDF export UI fixture\n', flush: true);
      final evidence = Directory(p.join(documents.path, 'PdfExportEvidence'));
      await evidence.create(recursive: true);
      final inputDirectory = Directory(
        p.join(documents.path, 'Issue5538Input'),
      );
      final config =
          jsonDecode(
                await File(
                  p.join(inputDirectory.path, 'input.json'),
                ).readAsString(),
              )
              as Map;
      final input = File(p.join(inputDirectory.path, 'source.pdf'));
      final inputDigest = (await sha256.bind(input.openRead()).first)
          .toString();
      if (inputDigest != config['sha256']) {
        throw StateError('Pinned real PDF changed in the Simulator container.');
      }
      final temporary = await getTemporaryDirectory();
      await pdfrxInitialize(tmpPath: temporary.path);
      final source = await PdfDocument.openFile(input.path);
      final fixture = File(p.join(temporary.path, 'issue5538-large-nup.pdf'));
      final clock = Stopwatch()..start();
      try {
        final count = source.pages.length;
        final length = await composePdfSheetsToFile(
          source,
          outputPath: fixture.path,
          firstIndex: 0,
          lastIndex: count - 1,
          rows: 2,
          cols: 2,
          sheetWidth: 595,
          sheetHeight: 842,
        );
        clock.stop();
        if (length <= 16 * 1024 * 1024 || length != await fixture.length()) {
          throw StateError('Large real N-up fixture is missing or <=16 MiB.');
        }
        final composed = await PdfDocument.openFile(fixture.path);
        try {
          if (composed.pages.length != (count + 3) ~/ 4) {
            throw StateError('Composed page count differs.');
          }
          _composition = {
            'fixtureKind': 'real-scanned-pdf-4-up',
            'sourceSha256': inputDigest,
            'sourceBytes': await input.length(),
            'sourcePages': count,
            'sheetCount': composed.pages.length,
            'composeUs': clock.elapsedMicroseconds,
            'rows': 2,
            'cols': 2,
          };
        } finally {
          await composed.dispose();
        }
      } finally {
        await source.dispose();
      }
      _fixture = fixture;
      _destination = destination;
      _evidence = evidence;
      if (mounted) setState(() => _state = 'ready');
    } catch (error) {
      if (mounted) {
        setState(() {
          _state = 'failed';
          _summary = 'prepare: $error';
        });
      }
    }
  }

  Future<void> _export(String caseName) async {
    if (_state != 'ready' || _operation != null) return;
    final fixture = _fixture!;
    final fileName = 'ios-export-$caseName.pdf';
    String? stagingPath;
    setState(() => _state = 'pending');
    final operation = MobilePdfExportService.instance.start(
      fileName: fileName,
      producer: (path) async {
        stagingPath = path;
        await fixture.copy(path);
      },
    );
    _operation = operation;
    try {
      final outcome = await operation.result;
      final source = stagingPath;
      final stagingAbsent =
          source != null && !await Directory(p.dirname(source)).exists();
      final expectedDigest = (await sha256.bind(fixture.openRead()).first)
          .toString();
      String? actualDigest;
      bool destinationVerified = false;
      if (outcome.status == MobilePdfExportStatus.saved &&
          outcome.documentUri != null) {
        final uri = Uri.parse(outcome.documentUri!);
        if (uri.scheme == 'file') {
          final saved = File.fromUri(uri);
          final canonical = await saved.resolveSymbolicLinks();
          final directory = await _destination!.resolveSymbolicLinks();
          actualDigest = (await sha256.bind(saved.openRead()).first).toString();
          destinationVerified =
              p.dirname(canonical) == directory &&
              p.basename(canonical) == outcome.displayName &&
              actualDigest == expectedDigest;
        }
      } else if (outcome.status == MobilePdfExportStatus.cancelled) {
        destinationVerified = !await File(
          p.join(_destination!.path, fileName),
        ).exists();
      }
      final valid =
          destinationVerified &&
          stagingAbsent &&
          outcome.cleanupStatus == MobilePdfExportCleanupStatus.notNeeded &&
          outcome.cleanupError == null &&
          outcome.residualDocument == null;
      final record = {
        'schemaVersion': 1,
        'case': caseName,
        'backend': 'MobilePdfExportService.instance',
        'picker': 'UIDocumentPickerViewController.forExporting.asCopy',
        'outcome': outcome.toMap(),
        'fixtureBytes': await fixture.length(),
        'composition': _composition,
        'expectedSha256': expectedDigest,
        'actualSha256': actualDigest,
        'destinationVerified': destinationVerified,
        'stagingAbsent': stagingAbsent,
        'passed': valid,
      };
      await File(p.join(_evidence!.path, '$caseName.json')).writeAsString(
        '${const JsonEncoder.withIndent('  ').convert(record)}\n',
        flush: true,
      );
      if (mounted) {
        setState(() {
          _state = outcome.status.name;
          _summary =
              'validated=$valid;stagingAbsent=$stagingAbsent;status=${outcome.status.name}';
          if (outcome.errorCode != null) {
            _summary += ';errorCode=${outcome.errorCode}';
          }
        });
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _state = 'failed';
          _summary = 'export: $error';
        });
      }
    }
  }

  @override
  void dispose() {
    unawaited(_operation?.cancel());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('PDF export native test')),
    body: Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Semantics(
            identifier: 'export-status',
            label: _state,
            child: ExcludeSemantics(child: Text('State: $_state')),
          ),
          Semantics(
            identifier: 'export-evidence',
            label: _summary,
            child: ExcludeSemantics(child: Text(_summary)),
          ),
          const SizedBox(height: 24),
          for (final caseName in ['files-save', 'files-cancel'])
            FilledButton(
              onPressed: _state == 'ready' ? () => _export(caseName) : null,
              child: Text('Export $caseName fixture'),
            ),
          const Text(
            'Destination: On My iPhone / אוצריא / OtzariaExportUITest',
          ),
        ],
      ),
    ),
  );
}
