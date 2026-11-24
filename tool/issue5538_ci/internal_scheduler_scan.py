"""Count actual worker cancellation reads separately from AOT timings."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import internal_upstream_run as common

WORKER_HASHES = {
    "bfa4467064f046e223c0dd05f1217414b51bf9bc1287c96340d053a090e6cda5",
    "a32a2970c344f25d221ec6921ef7fe7fac8c549a7295e409d6131cabc007419e",
}

HARNESS = r"""
int _ordinaryCancellationReads = 0;
int _ordinaryCompletions = 0;
int _cooperativeCancellationReads = 0;
int _cooperativeSteps = 0;
int _cooperativeDisposals = 0;

void main(List<String> args) {
  final mode = args[0];
  final count = int.parse(args[1]);
  final steps = int.parse(args[2]);
  // This probe counts scheduler operations, not PDFium or process-gate work.
  Pdfrx.useProcessWidePdfiumGate = false;
  final port = ReceivePort();
  final scheduler = _WorkerScheduler();
  var cooperativeRequests = 0;
  for (var i = 0; i < count; i++) {
    if (mode == 'cooperative' || (mode == 'mixed' && i % 100 == 0)) {
      cooperativeRequests++;
      scheduler.enqueue(_TaskExecuteParams<int, int>(
        port.sendPort, _ScanTask.new, steps, BackgroundWorkerPriority.normal,
      ));
    } else {
      scheduler.enqueue(_ExecuteParams<void, void>(
        port.sendPort, (_) { _ordinaryCompletions++; }, null, BackgroundWorkerPriority.normal,
      ));
    }
  }
  scheduler.drainAll();
  port.close();
  if (_ordinaryCompletions != count - cooperativeRequests ||
      _cooperativeSteps != cooperativeRequests * steps ||
      _cooperativeDisposals != cooperativeRequests) {
    throw StateError('Incomplete or duplicate work in scan probe.');
  }
  stdout.writeln(jsonEncode({
    'mode': mode,
    'requests': count,
    'steps': steps,
    'cooperativeRequests': cooperativeRequests,
    'ordinaryCancellationReads': _ordinaryCancellationReads,
    'cooperativeCancellationReads': _cooperativeCancellationReads,
    'ordinaryCompletions': _ordinaryCompletions,
    'cooperativeSteps': _cooperativeSteps,
    'cooperativeDisposals': _cooperativeDisposals,
  }));
}

class _ScanTask extends BackgroundWorkerTask<int> {
  _ScanTask(this.steps);
  final int steps;
  int completed = 0;

  @override
  bool get isCanceled {
    _cooperativeCancellationReads++;
    return false;
  }

  @override
  bool run() {
    _cooperativeSteps++;
    return ++completed == steps;
  }

  @override
  int get result => completed;

  @override
  void dispose() => _cooperativeDisposals++;
}
"""


def instrument_source(source):
    before, separator, requests = source.partition("class _ComputeParams {")
    common.require(separator, "Cannot identify ordinary requests")
    params, end, rest = requests.partition("class _ExecuteParams")
    getter = "bool get isCanceled => false;"
    common.require(end and params.count(getter) == 1, "Ordinary cancellation getter changed")
    params = params.replace(getter, "bool get isCanceled { _ordinaryCancellationReads++; return false; }")
    instrumented = before + separator + params + end + rest
    relative_import = "import '../pdfrx.dart';"
    common.require(instrumented.count(relative_import) == 1, "Worker configuration import changed")
    instrumented = instrumented.replace(relative_import, "import 'package:pdfrx_engine/src/pdfrx.dart';")
    return "import 'dart:convert';\nimport 'dart:io';\n" + instrumented + HARNESS


def retain(path, data):
    if path.exists():
        common.require(path.read_bytes() == data, "Existing scan receipt differs: " + path.name)
    else:
        path.write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("normal", "mixed", "cooperative"))
    parser.add_argument("requests", type=int)
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dart", type=Path, required=True)
    parser.add_argument("--receipt-directory", type=Path, required=True)
    args = parser.parse_args()
    common.require(os.environ.get("CI") == "true", "CI=true is required")
    common.require(args.requests > 0 and args.steps > 0, "Requests and steps must be positive")
    source_bytes = args.source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    common.require(source_hash in WORKER_HASHES, "Unrecognized worker source; refusing instrumentation")
    source = source_bytes.decode("utf-8")
    instrumented = instrument_source(source)
    receipt = args.receipt_directory.resolve()
    receipt.mkdir(parents=True, exist_ok=True)
    retain(receipt / "worker-source.dart", source_bytes)
    retain(receipt / "worker-instrumented.dart", instrumented.encode("utf-8"))
    patch = "".join(difflib.unified_diff(source.splitlines(True), instrumented.splitlines(True),
                                       fromfile="worker.dart", tofile="worker-instrumented.dart"))
    retain(receipt / "instrumentation.patch", patch.encode("utf-8"))
    package = args.engine_root.resolve()
    temporary = package / ".dart_tool"
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="issue5538-scan-", dir=temporary) as directory:
        entry = Path(directory) / "probe.dart"
        entry.write_text(instrumented, encoding="utf-8")
        result = subprocess.run(
            [str(args.dart), "run", str(entry), args.mode, str(args.requests), str(args.steps)],
            cwd=package, env={**os.environ, "CI": "true"}, capture_output=True, text=True, timeout=300)
    name = f"{args.mode}-{args.requests}"
    (receipt / (name + ".stdout.log")).write_text(result.stdout, encoding="utf-8")
    (receipt / (name + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
    common.require(result.returncode == 0, f"Scan failed: {result.stderr[-4000:]}")
    records = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and "ordinaryCancellationReads" in item:
            records.append(item)
    common.require(len(records) == 1, "Scan must report one complete observation")
    sample = records[0]
    sample.update({
        "workerSourceSha256": source_hash,
        "instrumentedSourceSha256": hashlib.sha256(instrumented.encode()).hexdigest(),
        "instrumentationPatchSha256": hashlib.sha256(patch.encode()).hexdigest(),
        "timingMeasurement": False,
    })
    print(json.dumps(sample, sort_keys=True))


if __name__ == "__main__":
    main()

