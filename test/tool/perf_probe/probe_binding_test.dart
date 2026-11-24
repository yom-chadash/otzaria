import 'dart:convert';
import 'dart:io';

import 'package:test/test.dart';

import '../../../tool/perf_probe/src/probe_binding.dart';
import '../../../tool/perf_probe/src/probe_completion.dart';

void main() {
  test(
    'the integration binding has explicit target and scheduler identity',
    () {
      expect(PerfBindingMode.parse('integration'), PerfBindingMode.integration);
      expect(PerfBindingMode.integration.toComparisonJson(), {
        'bindingMode': 'integration',
        'bindingClass': 'IntegrationTestWidgetsFlutterBinding',
        'framePolicy': 'fullyLive',
        'platformDispatcher': 'test',
        'probeTarget': 'integration_test/perf_resource_probe_test.dart',
      });
    },
  );

  test('only the general integration workload remains supported', () {
    expect(
      () => PerfBindingMode.integration.validateScenario('all'),
      returnsNormally,
    );
    for (final scenario in [
      'pdf-reader',
      'pdf-book-view',
      'pdf-multi-window',
      'unknown',
    ]) {
      expect(
        () => PerfBindingMode.integration.validateScenario(scenario),
        throwsArgumentError,
      );
    }
    expect(() => PerfBindingMode.parse('framework'), throwsArgumentError);
    expect(() => PerfBindingMode.parse('benchmarkLive'), throwsArgumentError);
  });

  group('atomic completion after the journal is closed', () {
    late Directory output;
    late File journal;
    final fixtureId = 'synthetic-v5-general:${List.filled(64, 'a').join()}';

    setUp(() async {
      output = await Directory.systemTemp.createTemp('perf-binding-contract-');
      journal = File('${output.path}/phases.jsonl');
      await journal.writeAsString(
        '{"type":"meta","launchIndex":2}\n',
        flush: true,
      );
    });
    tearDown(() async {
      await output.delete(recursive: true);
    });

    Future<void> complete({
      PerfBindingMode mode = PerfBindingMode.integration,
      bool successful = true,
      String? error,
      String? fixture,
      bool missingFixture = false,
      bool preparationOnly = false,
      List<String> failures = const [],
    }) => writePerfProbeCompletion(
      output: output,
      launchIndex: 2,
      bindingMode: mode,
      successful: successful,
      error: error,
      fixtureId: missingFixture ? null : fixture ?? fixtureId,
      preparationOnly: preparationOnly,
      testFailures: failures,
    );

    Future<Map<String, dynamic>> result() async =>
        jsonDecode(await File('${output.path}/completion.json').readAsString())
            as Map<String, dynamic>;

    test('success binds the host handshake to the requested target', () async {
      await complete();
      final value = await result();
      expect(value['status'], 'passed');
      expect(value['fixtureId'], fixtureId);
      expect(value['testFailures'], isEmpty);
      for (final entry
          in PerfBindingMode.integration.toComparisonJson().entries) {
        expect(value[entry.key], entry.value);
      }
      final rows = (await journal.readAsLines())
          .map((line) => jsonDecode(line) as Map<String, dynamic>)
          .toList();
      expect(rows, hasLength(2));
      expect(rows.last['type'], 'done');
      expect(rows.last['launchIndex'], 2);
      expect(rows.last['status'], 'passed');
      expect(await File('${output.path}/completion.pending').exists(), isFalse);
    });

    test(
      'integration test failures cannot publish a successful handshake',
      () async {
        await complete(
          mode: PerfBindingMode.integration,
          failures: ['late test failure'],
        );
        final value = await result();
        expect(value['status'], 'failed');
        expect(value['testFailures'], ['late test failure']);
        expect(value['framePolicy'], 'fullyLive');
        expect(value['bindingMode'], 'integration');
      },
    );

    test(
      'an action error cannot be hidden by successful caller state',
      () async {
        await complete(error: 'action failed');
        final value = await result();
        expect(value['status'], 'failed');
        expect(value['error'], 'action failed');
      },
    );

    test('fixture failure before the journal is reported', () async {
      await journal.delete();
      await complete(
        successful: false,
        error: 'invalid fixture',
        missingFixture: true,
      );
      final value = await result();
      expect(value['status'], 'failed');
      expect(value['error'], 'invalid fixture');
      expect(value['fixtureId'], isNull);
      final done =
          jsonDecode(await journal.readAsString()) as Map<String, dynamic>;
      expect(done['type'], 'done');
      expect(done['status'], 'failed');
    });

    test('success without a fixture cannot append a done record', () async {
      await expectLater(complete(missingFixture: true), throwsStateError);
      expect(await journal.readAsLines(), hasLength(1));
      expect(await File('${output.path}/completion.json').exists(), isFalse);
    });

    test('an existing completion is never overwritten', () async {
      final marker = File('${output.path}/completion.json');
      await marker.writeAsString('previous run');
      await expectLater(complete(), throwsStateError);
      expect(await marker.readAsString(), 'previous run');
      expect(await journal.readAsLines(), hasLength(1));
    });

    test('an unfinished prior completion is never reused', () async {
      final pending = File('${output.path}/completion.pending');
      await pending.writeAsString('unfinished run');
      await expectLater(complete(), throwsStateError);
      expect(await pending.readAsString(), 'unfinished run');
      expect(await File('${output.path}/completion.json').exists(), isFalse);
    });

    test('success requires an existing journal', () async {
      await journal.delete();
      await expectLater(complete(), throwsStateError);
      expect(await File('${output.path}/completion.json').exists(), isFalse);
    });

    test(
      'fixture preparation has explicit identity but is not a measured launch',
      () async {
        await complete(preparationOnly: true);
        final value = await result();
        expect(value['status'], 'passed');
        expect(value['preparationOnly'], isTrue);
        expect(value['bindingMode'], 'integration');
      },
    );
  });
}
