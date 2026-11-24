import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import '../../tool/perf_probe/src/probe_binding.dart';

void verifyPerfProbeBinding(WidgetsBinding binding, PerfBindingMode mode) {
  final dispatcher = binding.platformDispatcher;
  final matches = switch (mode) {
    PerfBindingMode.integration =>
      binding.runtimeType == IntegrationTestWidgetsFlutterBinding &&
          binding is LiveTestWidgetsFlutterBinding &&
          binding.framePolicy ==
              LiveTestWidgetsFlutterBindingFramePolicy.fullyLive &&
          dispatcher is TestPlatformDispatcher,
  };
  if (!identical(binding, WidgetsBinding.instance) || !matches) {
    throw StateError('Unexpected binding or dispatcher for ${mode.name}.');
  }
}
