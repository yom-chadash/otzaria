// Functional desktop smoke using real OS windows and Flutter's native dispatcher.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_bloc/flutter_bloc.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:hive_ce/hive.dart';
import 'package:otzaria/app.dart';
import 'package:otzaria/core/error_log_file.dart';
import 'package:otzaria/core/startup_timeline.dart';
import 'package:otzaria/data/data_providers/hive_data_provider.dart';
import 'package:otzaria/data/repository/data_repository.dart';
import 'package:otzaria/main.dart' as app;
import 'package:otzaria/settings/engine/settings_repository.dart';
import 'package:otzaria/tabs/bloc/tabs_bloc.dart';
import 'package:otzaria/tabs/bloc/tabs_event.dart';
import 'package:path/path.dart' as p;

import '../tool/perf_probe/src/probe_engine.dart';
import 'support/installed_plugin_fixture.dart' show rejectInstalledProbeLinks;
import 'support/installed_plugin_pdf_fixture.dart';
import 'support/pdf_multi_window_scenario.dart';
import 'support/perf_fixture.dart';
import 'support/perf_probe_wait.dart';

const _source = String.fromEnvironment('PDF_MULTI_WINDOW_SOURCE');
const _engineRevision = String.fromEnvironment(
  'PDF_MULTI_WINDOW_ENGINE_REVISION',
);
const _mode = kReleaseMode ? 'release' : (kProfileMode ? 'profile' : 'debug');

void _require(bool value, String message) {
  if (!value) throw StateError(message);
}

void _verifyBinding(WidgetsBinding binding) {
  _require(
    identical(binding, WidgetsBinding.instance) &&
        binding.runtimeType == WidgetsFlutterBinding &&
        binding.platformDispatcher is! TestPlatformDispatcher,
    'The smoke requires WidgetsFlutterBinding and a real platform dispatcher.',
  );
}

Map<String, Object?> _engineIdentity(String source) {
  if (Platform.isWindows) {
    final identity = parsePerfEngineIdentity(
      source,
      buildMode: _mode,
      platform: 'windows',
    );
    _require(identity != null, 'Missing Windows engine identity.');
    _require(
      identity!['engineRevision'] == _engineRevision,
      'Compiled and supplied engine revisions differ.',
    );
    return identity;
  }
  final decoded = jsonDecode(source);
  _require(decoded is Map<String, dynamic>, 'Invalid engine identity JSON.');
  final value = decoded as Map<String, dynamic>;
  const keys = {
    'schemaVersion',
    'platform',
    'buildMode',
    'binaryName',
    'bytes',
    'bundleSha256',
  };
  _require(
    value.keys.toSet().difference(keys).isEmpty &&
        value.length == keys.length &&
        value['schemaVersion'] == 1 &&
        value['schemaVersion'] is int &&
        value['platform'] == Platform.operatingSystem &&
        value['buildMode'] == _mode &&
        value['binaryName'] ==
            (Platform.isLinux ? 'libflutter_linux_gtk.so' : 'FlutterMacOS') &&
        value['bytes'] is int &&
        (value['bytes'] as int) > 0 &&
        value['bundleSha256'] is String &&
        RegExp(r'^[a-f0-9]{64}$').hasMatch(value['bundleSha256'] as String),
    'The engine receipt does not identify this desktop build.',
  );
  return Map<String, Object?>.unmodifiable(value);
}

Future<void> _writeCompletion(
  Directory output,
  Map<String, Object?> value,
) async {
  final pending = File(p.join(output.path, 'completion.pending'));
  await pending.writeAsString(jsonEncode(value), flush: true);
  await pending.rename(p.join(output.path, 'completion.json'));
}

Future<void> _run() async {
  final binding = WidgetsFlutterBinding.ensureInitialized();
  final output = Directory(Platform.environment['PDF_MULTI_WINDOW_OUTPUT']!);
  final data = Directory(Platform.environment['PDF_MULTI_WINDOW_DATA']!);
  _require(
    p.isAbsolute(output.path) &&
        p.isAbsolute(data.path) &&
        !p.equals(output.path, data.path) &&
        !p.isWithin(output.path, data.path) &&
        !p.isWithin(data.path, output.path),
    'Set separate absolute smoke output and fixture directories.',
  );
  await rejectInstalledProbeLinks(output.path);
  await rejectInstalledProbeLinks(data.path);
  await output.create(recursive: true);
  _require(await output.list().isEmpty, 'Smoke output must be new or empty.');
  final unhandled = <String>[];
  final completion = <String, Object?>{
    'schemaVersion': 1,
    'status': 'failed',
    'sourceSha': _source,
    'engineRevision': _engineRevision,
    'buildMode': _mode,
    'bindingClass': binding.runtimeType.toString(),
    'nativePlatformDispatcher':
        binding.platformDispatcher is! TestPlatformDispatcher,
    'unhandledErrors': unhandled,
    'error': null,
  };
  try {
    _require(Platform.environment['CI'] == 'true', 'CI=true is required.');
    _require(
      (Platform.isLinux || Platform.isMacOS || Platform.isWindows) &&
          const {'profile', 'release'}.contains(_mode),
      'The smoke requires a profile or release desktop build.',
    );
    _require(
      RegExp(r'^[a-f0-9]{40}$').hasMatch(_source) &&
          Platform.environment['PDF_MULTI_WINDOW_SOURCE'] == _source &&
          RegExp(r'^[a-f0-9]{40}$').hasMatch(_engineRevision),
      'Compiled source or engine identity is missing or differs from the host.',
    );
    completion['engineIdentity'] = _engineIdentity(
      Platform.environment['PDF_MULTI_WINDOW_ENGINE_JSON']!,
    );
    _verifyBinding(binding);
    final fixture = PerfFixture(data);
    await fixture.prepare(preparationOnly: true);
    final settings = await Hive.openBox<dynamic>(
      HiveCache.keyName,
      path: fixture.appDataPath,
    );
    try {
      await settings.put(SettingsRepository.keyPdfBookViewByDefault, false);
    } finally {
      await settings.close();
    }
    final pdf = File(p.join(data.path, 'multi-window.pdf'));
    final bytes = createInstalledPdfFixture();
    await pdf.writeAsBytes(bytes, flush: true);
    final input = PdfMultiWindowInput(
      pdf.path,
      sha256.convert(bytes).toString(),
    );
    final errorsFile = ErrorLogFile.resolveFile();
    final errorsBefore = await errorsFile.exists()
        ? await errorsFile.readAsString()
        : '';
    FlutterExceptionHandler? previousFlutterError;
    void onFlutterError(FlutterErrorDetails details) {
      unhandled.add(details.exceptionAsString());
      previousFlutterError?.call(details);
    }

    bool onPlatformError(Object error, StackTrace stack) {
      onFlutterError(FlutterErrorDetails(exception: error, stack: stack));
      return true;
    }

    void captureErrors() {
      if (FlutterError.onError != onFlutterError) {
        previousFlutterError = FlutterError.onError;
        FlutterError.onError = onFlutterError;
      }
      ui.PlatformDispatcher.instance.onError = onPlatformError;
    }

    captureErrors();
    app.main(const []);
    await waitForPdfProbeCondition(
      () {
        captureErrors();
        return binding.rootElement != null &&
            find.byType(App).evaluate().isNotEmpty &&
            StartupTimeline.instance.toJson()['revealMs'] != null;
      },
      'actual app first content',
      timeout: const Duration(minutes: 3),
    );
    await DataRepository.instance.library;
    final driver = LiveWidgetController(binding);
    final context = driver.element(find.byType(App));
    final tabs = context.read<TabsBloc>();
    if (tabs.state.tabs.isNotEmpty) {
      tabs.add(RemoveTabs(tabs.state.tabs.toList()));
      await waitForPdfProbeCondition(
        () => tabs.state.tabs.isEmpty,
        'owned startup tabs closed',
        advance: driver.pump,
      );
    }
    await runPdfMultiWindowSmoke(
      controller: driver,
      context: context,
      input: input,
      output: output,
    );
    _verifyBinding(binding);
    final errorsAfter = await errorsFile.exists()
        ? await errorsFile.readAsString()
        : '';
    final newErrors = errorsAfter.startsWith(errorsBefore)
        ? errorsAfter.substring(errorsBefore.length)
        : errorsAfter;
    _require(
      !RegExp(
        r'^=== (FlutterError|Unhandled Error)',
        multiLine: true,
      ).hasMatch(newErrors),
      'The app logged an unhandled error during the smoke.',
    );
    _require(unhandled.isEmpty, 'Unhandled application errors occurred.');
    final report = File(p.join(output.path, 'report.json'));
    completion['reportSha256'] = (await sha256.bind(report.openRead()).single)
        .toString();
    completion['status'] = 'passed';
  } catch (error, stack) {
    completion['error'] = '$error';
    stderr.writeln('$error\n$stack');
  }
  await _writeCompletion(output, completion);
  if (completion['status'] == 'passed') {
    try {
      await waitForPdfProbeCondition(
        () => File(p.join(output.path, 'stop')).existsSync(),
        'host checked the owned process and bundled engine',
        timeout: const Duration(seconds: 60),
      );
      _require(
        unhandled.isEmpty,
        'Application errors occurred after completion.',
      );
    } catch (error) {
      completion['status'] = 'failed';
      completion['error'] = '$error';
      await _writeCompletion(output, completion);
    }
  }
  exit(completion['status'] == 'passed' ? 0 : 1);
}

void main() {
  unawaited(
    runZonedGuarded<Future<void>>(_run, (error, stack) {
      stderr.writeln('Unhandled native window smoke error: $error\n$stack');
      exit(1);
    }),
  );
}
