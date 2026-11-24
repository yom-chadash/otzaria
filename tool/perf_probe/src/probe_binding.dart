/// Scheduling identity is part of the comparison, not a performance threshold.
enum PerfBindingMode {
  integration(
    target: 'integration_test/perf_resource_probe_test.dart',
    bindingClass: 'IntegrationTestWidgetsFlutterBinding',
    framePolicy: 'fullyLive',
    platformDispatcher: 'test',
  );

  const PerfBindingMode({
    required this.target,
    required this.bindingClass,
    required this.framePolicy,
    required this.platformDispatcher,
  });

  final String target;
  final String bindingClass;
  final String framePolicy;
  final String platformDispatcher;

  static PerfBindingMode parse(String value) => switch (value) {
    'integration' => integration,
    _ => throw ArgumentError.value(value, 'bindingMode'),
  };

  void validateScenario(String scenario) {
    if (scenario != 'all') {
      throw ArgumentError(
        'The $name binding does not support scenario "$scenario".',
      );
    }
  }

  Map<String, Object?> toComparisonJson() => {
    'bindingMode': name,
    'bindingClass': bindingClass,
    'framePolicy': framePolicy,
    'platformDispatcher': platformDispatcher,
    'probeTarget': target,
  };

  List<String> get limitations => const [
    'Post-close observation means no user actions under the integration binding, not quiescent production idle.',
    'The fullyLive integration binding schedules continuous frames; CPU and frame samples include that harness work.',
  ];
}
