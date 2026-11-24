// ignore: depend_on_referenced_packages, implementation_imports
import 'package:pdfrx_engine/src/native/worker.dart';

Future<List<Map<String, Object?>>> takeTextMetrics({required bool reset}) =>
    BackgroundWorker.compute((_) => <Map<String, Object?>>[], reset);
