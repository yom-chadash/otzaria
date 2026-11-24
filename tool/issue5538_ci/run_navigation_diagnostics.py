"""Reproduce MG book navigation with unchanged handlers; diagnostic data only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tool.perf_probe import build_receipt
from tool.perf_probe import run_pdf_pair as pair
from tool.perf_probe.paired_pdf_case import PdfCaseSnapshot


def run(args):
    source, output = args.source.resolve(), args.output.resolve()
    commit = pair.full_sha(args.commit)
    if platform.system() != 'Windows':
        raise ValueError('The observed failure was Windows profile; this diagnostic keeps that platform.')
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Diagnostic results and owned worktree must be outside the orchestrator.')
    if not 1 <= args.launches <= 3:
        raise ValueError('Diagnostics accept one to three independent cold launches.')
    orchestrator = pair.git(source, 'rev-parse', 'HEAD')
    pair.verify_checkout(source, orchestrator)
    # Guard every existing common harness and dependency object, including the
    # public controller observer. No source patch is applied at measurement time.
    for path in (*pair.HARNESS_PATHS, *pair.DEPENDENCY_PATHS, 'lib/pdf_book/view/pdf_book_screen.dart'):
        if pair.git(source, 'rev-parse', f'{commit}:{path}') != pair.git(source, 'rev-parse', f'{orchestrator}:{path}'):
            raise ValueError(f'Diagnostic source/orchestrator harness differs: {path}')
    flutter, dart, pwsh = (shutil.which(name) for name in ('flutter', 'dart', 'pwsh'))
    if not all((flutter, dart, pwsh)):
        raise ValueError('Pinned Flutter/Dart and PowerShell are required.')
    output.mkdir(parents=True, exist_ok=False)
    logs = output / 'logs'
    logs.mkdir()
    checkout = output / 'source'
    frozen = output / 'native-build.json'
    summary = {
        'schemaVersion': 1, 'status': 'running', 'diagnosticOnly': True,
        'performanceComparable': False, 'declaredScopeComplete': False,
        'sourceCommit': commit, 'orchestratorCommit': orchestrator,
        'sourceTree': pair.git(source, 'rev-parse', f'{commit}^{{tree}}'),
        'platform': 'Windows', 'mode': 'profile', 'scenario': 'pdf-book-view',
        'cacheState': 'cold', 'warmup': 1, 'repetitions': 5,
        'requestedIndependentLaunches': args.launches, 'attempts': [],
        'limitations': [
            'Diagnostic controller observers and stack capture add overhead; no timing comparison is permitted.',
            'The original application navigation handlers, destinations, 3 s application cap and 45 s image observer remain unchanged.',
            'The controller returns each exact original Future; no retry, sleep, cancellation or timeout is added.',
            'Only public controller navigation methods are intercepted; engine-private calls are not claimed to be complete.',
        ],
        'workflow': {key: os.getenv(key) for key in ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB')},
    }
    def save():
        pair.write_json(output / 'diagnostic-summary.json', summary)
    created = False
    try:
        save()
        pair.git(source, 'worktree', 'add', '--detach', str(checkout), commit)
        created = True
        pair.verify_checkout(checkout, commit)
        # Flutter's first Windows invocation may bootstrap the tool and print
        # non-JSON startup output. Keep that explicit preparation in a log;
        # the subsequent machine identity still uses the strict JSON parser.
        pair.run_logged([flutter, '--version'], checkout, logs / 'sdk-bootstrap.log')
        pair.verify_checkout(checkout, commit)
        summary['sdk'] = pair.sdk_identity(flutter, checkout)
        for name, arguments in (
            ('resolve', ['pub', 'get', '--enforce-lockfile']),
            ('analyze', ['analyze', '--no-pub', 'integration_test/support/pdf_navigation_trace.dart',
                         'integration_test/support/perf_resource_workload.dart',
                         'lib/pdf_book/view/pdf_book_screen.dart',
                         'test/tool/perf_probe/pdf_navigation_trace_test.dart']),
            ('diagnostic-contracts', ['test', '--no-pub', '--reporter', 'expanded',
                                      'test/tool/perf_probe/pdf_navigation_trace_test.dart']),
        ):
            pair.run_logged([flutter, *arguments], checkout, logs / f'{name}.log')
            pair.verify_checkout(checkout, commit)
        def canonicalize(request, target):
            pair.run_logged([dart, 'tool/perf_probe/prepare_pdf_case.dart', str(request), str(target)],
                            checkout, logs / 'prepare-pdf-case.log')
        case = PdfCaseSnapshot.prepare(args.pdf_case.absolute(), output, canonicalize)
        if (case.case['source']['pageCount'] != 374 or
                case.case['source'].get('sha256') != '92a145512e33c4a3c8f1b1c92d2d79c4db91a49cc921a316201be81bfa85040d' or
                case.case['navigation']['forwardPage'] != 280):
            raise ValueError('This diagnostic requires the same pinned MG374 PDF and target page280.')
        summary['pdfCase'] = case.receipt
        native = argparse.Namespace(mode='profile', cache_state='cold', scenario='pdf-book-view',
                                    launches=1, repetitions=5, expected_engine_sha256='', display_mode='desktop')
        frozen_sha = ''
        for index in range(args.launches):
            case.verify()
            pair.verify_checkout(checkout, commit)
            if pair.sdk_identity(flutter, checkout) != summary['sdk']:
                raise ValueError('SDK identity changed between diagnostic processes.')
            result = output / f'attempt-{index + 1}'
            policy = 'create' if index == 0 else 'reuse'
            command = pair.native_arguments(checkout, native, result, case.path,
                f'navigation-diagnostic-{platform.node()}', 'Windows', pwsh,
                build_receipt=frozen, build_policy=policy, build_receipt_sha256=frozen_sha)
            with (logs / f'attempt-{index + 1}.log').open('wb') as stream:
                process = subprocess.run(command, cwd=checkout, stdout=stream, stderr=subprocess.STDOUT,
                                         env={**os.environ, 'PERF_NAVIGATION_TRACE': '1'})
            attempt = {'index': index + 1, 'exitCode': process.returncode, 'buildPolicy': policy}
            summary['attempts'].append(attempt)
            save()
            pair.verify_checkout(checkout, commit)
            case.verify()
            completion_path = result / 'launch-000/completion.json'
            completion = pair.read_json(completion_path) if completion_path.is_file() else {}
            manifest = pair.read_json(result / 'manifest.json') if (result / 'manifest.json').is_file() else {}
            phases_path = result / 'launch-000/phases.jsonl'
            phases = ([json.loads(line) for line in phases_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
                      if phases_path.is_file() else [])
            failures = [{key: row.get(key) for key in ('name', 'warmup', 'iteration', 'durationMs', 'error')}
                        for row in phases if row.get('type') == 'phase' and row.get('status') == 'failed']
            attempt['failedPhases'] = failures
            expected_navigation_failure = (process.returncode != 0 and completion.get('status') == 'failed'
                and str(manifest.get('error', '')).startswith('Benchmark failed; inspect ')
                and any(str(row['name']).startswith('pdf.book_view.jump_') for row in failures))
            if process.returncode != 0 and not expected_navigation_failure:
                raise RuntimeError(f'Diagnostic attempt{index + 1} failed outside a completed app failure; no further launch is permitted.')
            # A failed app phase is retained, then another fresh process may
            # investigate reproducibility. Provenance/cleanup failures never retry.
            if not frozen_sha:
                frozen_sha = hashlib.sha256(frozen.read_bytes()).hexdigest()
            spec = build_receipt.specification(checkout, 'windows', 'profile', 'framework')
            proof = build_receipt.verify(spec, frozen, frozen_sha,
                                         build_receipt.expected_executable(checkout, 'windows', 'profile'))
            build_receipt.write_new(result / 'build-proof-diagnostic-after-process.json', proof)
            traces = sorted((result / 'launch-000').glob('navigation-*.json'))
            if not traces:
                raise RuntimeError('Native app produced no diagnostic navigation receipt.')
            for trace_path in traces:
                trace = pair.read_json(trace_path)
                if trace.get('diagnosticOnly') is not True or trace.get('performanceComparable') is not False:
                    raise RuntimeError('Diagnostic-only declaration is missing.')
                events = trace.get('records', [])
                if (not any(row.get('event') == 'owner.attachment.verified' for row in events)
                        or not any(row.get('event') == 'navigation.call' for row in events)):
                    raise RuntimeError('Diagnostic controller was not attached or did not observe actual navigation.')
            attempt.update(status=completion.get('status'), error=completion.get('error'),
                           receiptSha256=frozen_sha, traceFiles=[str(path.relative_to(output)) for path in traces])
            save()
        summary['declaredScopeComplete'] = True
        summary['navigationFailuresObserved'] = sum(value['status'] == 'failed' for value in summary['attempts'])
        summary['status'] = 'navigation-failure-observed' if summary['navigationFailuresObserved'] else 'not-reproduced'
    except Exception as error:
        summary.update(status='diagnostic-infrastructure-failure', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        try:
            if created:
                pair.git(source, 'worktree', 'remove', '--force', str(checkout))
        except Exception as error:
            summary['cleanupError'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            save()
    return 1 if summary['navigationFailuresObserved'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--pdf-case', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launches', type=int, default=3)
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
