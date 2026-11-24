import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:otzaria/pdf_book/utils/pdf_font_fallback.dart';
import 'package:otzaria/theme/app_fonts.dart';
import 'package:pdfrx/pdfrx.dart';

PdfFontQuery _font(String face) => PdfFontQuery(
  face: face,
  weight: 400,
  isItalic: false,
  charset: PdfFontCharset.hebrew,
  pitchFamily: 0,
);

class _Document extends Fake implements PdfDocument {
  _Document(this.sourceName, {this.isEncrypted = false, this.initial = const []});

  @override
  final String sourceName;
  @override
  final bool isEncrypted;
  final List<PdfFontQuery> initial;
  final controller = StreamController<PdfDocumentEvent>.broadcast(sync: true);
  int disposals = 0;

  @override
  Stream<PdfDocumentEvent> get events => Stream.multi((sink) {
    if (initial.isNotEmpty) {
      sink.add(PdfDocumentMissingFontsEvent(this, initial));
    }
    final subscription = controller.stream.listen(
      sink.add,
      onDone: sink.close,
    );
    sink.onCancel = subscription.cancel;
  }, isBroadcast: true);

  void missing(PdfFontQuery query) {
    controller.add(PdfDocumentMissingFontsEvent(this, [query]));
  }

  @override
  Future<void> dispose() async {
    if (controller.isClosed) return;
    disposals++;
    await controller.close();
  }
}

class _Backend extends Fake implements PdfrxEntryFunctions {
  final opened = <String, List<_Document>>{};
  final registered = <String>[];
  final gates = <String, Completer<void>>{};
  final initial = <String, List<PdfFontQuery>>{};
  final initialByAttempt = <String, Map<int, List<PdfFontQuery>>>{};
  final encrypted = <String>{};
  final failOpen = <String, Set<int>>{};
  int fontReloads = 0;

  @override
  Future<void> init() async {}

  @override
  Future<void> configureFontEnvironment({
    String? fontCachePath,
    List<String> fontPaths = const [],
  }) async {}

  @override
  Future<void> addFontData({
    required String face,
    required Uint8List data,
    String? resolvedFace,
  }) async {
    expect(data.length, greaterThan(4));
    registered.add(face);
    await gates[face]?.future;
  }

  @override
  Future<void> addFontFile({
    required String face,
    required String filePath,
    String? resolvedFace,
  }) async {
    registered.add(face);
    await gates[face]?.future;
  }

  @override
  Future<void> reloadFonts() async {
    fontReloads++;
  }

  @override
  Future<PdfDocument> openFile(
    String path, {
    PdfPasswordProvider? passwordProvider,
    bool firstAttemptByEmptyPassword = true,
    bool useProgressiveLoading = false,
  }) async {
    final documents = opened.putIfAbsent(path, () => []);
    final attempt = documents.length + 1;
    if (failOpen[path]?.remove(attempt) ?? false) {
      throw StateError('deliberate reopen failure');
    }
    if (encrypted.contains(path)) {
      expect(await passwordProvider?.call(), 'accepted-password');
    }
    final document = _Document(
      path,
      isEncrypted: encrypted.contains(path),
      initial: initialByAttempt[path]?[attempt] ??
          (attempt == 1 ? initial[path] ?? const [] : const []),
    );
    documents.add(document);
    return document;
  }
}

Future<void> _until(bool Function() condition) async {
  final limit = DateTime.now().add(const Duration(seconds: 5));
  while (!condition()) {
    if (DateTime.now().isAfter(limit)) fail('condition was not reached');
    await Future<void>.delayed(const Duration(milliseconds: 1));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late PdfrxEntryFunctions originalBackend;
  late _Backend backend;
  final listeners = <void Function()>[];
  final readers = <PdfFontFallbackReader>[];
  final originalCachePath = Pdfrx.cacheDirectoryPath;

  setUp(() {
    originalBackend = PdfrxEntryFunctions.instance;
    backend = _Backend();
    PdfrxEntryFunctions.instance = backend;
    Pdfrx.cacheDirectoryPath = Directory.systemTemp.path;
    AppFonts.debugSystemFontsHebrewCache = [];
  });

  tearDown(() async {
    for (final remove in listeners) {
      remove();
    }
    listeners.clear();
    for (final reader in readers) {
      await reader.dispose();
    }
    readers.clear();
    for (final gate in backend.gates.values) {
      if (!gate.isCompleted) gate.complete();
    }
    for (final documents in backend.opened.values) {
      for (final document in documents) {
        await document.dispose();
      }
    }
    await Future<void>.delayed(Duration.zero);
    PdfrxEntryFunctions.instance = originalBackend;
    Pdfrx.cacheDirectoryPath = originalCachePath;
    AppFonts.debugResetSystemFontsCache();
  });

  Future<PdfDocumentListenable> open(String path) async {
    final listenable = PdfFontFallback.documentRef(path).resolveListenable();
    listeners.add(listenable.addListener(() {}));
    await listenable.load();
    expect(listenable.error, isNull);
    return listenable;
  }

  test('גופן מאוחר מרענן רק את המקור שדיווח עליו', () async {
    final a = await open('late-a.pdf');
    final b = await open('late-b.pdf');
    final first = a.document! as _Document;
    first.missing(_font('OtzariaLifecycleLateA'));
    await _until(() => backend.opened['late-a.pdf']!.length == 2);
    await _until(() => first.disposals == 1);
    expect(backend.opened['late-b.pdf'], hasLength(1));
    expect(b.document, same(backend.opened['late-b.pdf']!.single));
    expect(first.controller.hasListener, isFalse);
  });

  test('סגירה בזמן טעינת גופן ראשוני אינה פותחת מסמך שוב', () async {
    final query = _font('OtzariaLifecycleInitialClose');
    final gate = Completer<void>();
    backend
      ..initial['initial-close.pdf'] = [query]
      ..gates[query.face] = gate;
    final ref = await open('initial-close.pdf');
    final first = ref.document! as _Document;
    await _until(() => backend.registered.contains(query.face));
    listeners.removeLast()();
    await _until(() => first.disposals == 1);
    gate.complete();
    await _until(() => backend.fontReloads == 1);
    await Future<void>.delayed(Duration.zero);
    expect(backend.opened['initial-close.pdf'], hasLength(1));
    expect(ref.document, isNull);
    expect(first.controller.hasListener, isFalse);
  });

  test('החלפת מסמך בזמן טעינה לא מרעננת את המחליף', () async {
    final query = _font('OtzariaLifecycleReplace');
    final gate = Completer<void>();
    backend.gates[query.face] = gate;
    final ref = await open('replace.pdf');
    final old = ref.document! as _Document;
    old.missing(query);
    await _until(() => backend.registered.contains(query.face));
    final replacement = _Document('another-source.pdf');
    addTearDown(replacement.dispose);
    ref.setDocument(replacement);
    await _until(() => old.disposals == 1);
    gate.complete();
    await _until(() => backend.fontReloads == 1);
    await Future<void>.delayed(Duration.zero);
    expect(ref.document, same(replacement));
    expect(backend.opened['replace.pdf'], hasLength(1));
    expect(replacement.disposals, 0);
  });

  test('reload שהתחיל דור חדש מבטל completion של הדור הקודם', () async {
    final query = _font('OtzariaLifecycleGeneration');
    final gate = Completer<void>();
    backend.gates[query.face] = gate;
    final ref = await open('generation.pdf');
    final old = ref.document! as _Document;
    old.missing(query);
    await _until(() => backend.registered.contains(query.face));
    await ref.load(forceReload: true);
    final replacement = ref.document;
    gate.complete();
    await _until(() => backend.fontReloads == 1);
    await Future<void>.delayed(Duration.zero);
    expect(ref.document, same(replacement));
    expect(backend.opened['generation.pdf'], hasLength(2));
  });

  test('אותו גופן מדווח אחרי רישומו עדיין מרענן את המסמך השני', () async {
    final a = await open('registered-a.pdf');
    final b = await open('registered-b.pdf');
    final query = _font('OtzariaLifecycleAlreadyRegistered');
    (a.document! as _Document).missing(query);
    await _until(() => backend.opened['registered-a.pdf']!.length == 2);
    (b.document! as _Document).missing(query);
    await _until(() => backend.opened['registered-b.pdf']!.length == 2);
    expect(backend.registered.where((face) => face == query.face), hasLength(1));
  });

  test('סיסמה שאושרה משמשת גם לשחזור המאוחר', () async {
    backend.encrypted.add('password.pdf');
    var requests = 0;
    final ref = PdfFontFallback.documentRef(
      'password.pdf',
      passwordProvider: () async {
        requests++;
        return 'accepted-password';
      },
    ).resolveListenable();
    listeners.add(ref.addListener(() {}));
    await ref.load();
    (ref.document! as _Document).missing(_font('OtzariaLifecyclePassword'));
    await _until(() => backend.opened['password.pdf']!.length == 2);
    expect(requests, 1);
  });

  test('חילוץ ממתין לסיום native לפני reopen וקורא שוב את אותו עמוד', () async {
    final reader = await PdfFontFallback.openReader('read.pdf');
    readers.add(reader);
    final old = reader.document as _Document;
    final native = Completer<String>();
    final query = _font('OtzariaLifecycleRead');
    final observed = <PdfDocument>[];
    final result = reader.read((document) async {
      observed.add(document);
      if (identical(document, old)) {
        old.missing(query);
        return native.future;
      }
      return 'שלום';
    });
    await _until(() => backend.registered.contains(query.face));
    expect(old.disposals, 0);
    expect(backend.opened['read.pdf'], hasLength(1));
    native.complete('missing-glyphs');
    expect(await result, 'שלום');
    expect(observed, hasLength(2));
    expect(observed.last, same(reader.document));
    expect(old.disposals, 1);
  });

  test('סגירת reader מנתקת fonts מיד אך סוגרת native אחרי הקריאה', () async {
    final reader = await PdfFontFallback.openReader('read-close.pdf');
    readers.add(reader);
    final old = reader.document as _Document;
    final native = Completer<String>();
    final began = Completer<void>();
    final result = reader.read((document) async {
      began.complete();
      return native.future;
    });
    await began.future;
    var disposed = false;
    final closing = reader.dispose().then((_) => disposed = true);
    await Future<void>.delayed(Duration.zero);
    expect(old.controller.hasListener, isFalse);
    expect(old.disposals, 0);
    expect(disposed, isFalse);
    native.complete('read-finished');
    expect(await result, 'read-finished');
    await closing;
    expect(old.disposals, 1);
    expect(backend.opened['read-close.pdf'], hasLength(1));
  });

  test('reopen כושל משאיר את המקור חי ומאפשר ניסיון חוזר', () async {
    final reader = await PdfFontFallback.openReader('read-retry.pdf');
    readers.add(reader);
    final old = reader.document as _Document;
    backend.failOpen['read-retry.pdf'] = {2};
    var reported = false;
    Future<String> read(PdfDocument document) async {
      if (identical(document, old) && !reported) {
        reported = true;
        old.missing(_font('OtzariaLifecycleRetry'));
      }
      return identical(document, old) ? 'before' : 'after';
    }
    await expectLater(reader.read(read), throwsStateError);
    expect(old.disposals, 0);
    expect(await reader.read(read), 'after');
    expect(old.disposals, 1);
  });

  test('סגירה מבטלת גם resolver אטי במסמך replacement שטרם נמסר', () async {
    final reader = await PdfFontFallback.openReader('replacement-close.pdf');
    readers.add(reader);
    final old = reader.document as _Document;
    final firstQuery = _font('OtzariaLifecycleReplacementFirst');
    final nextQuery = _font('OtzariaLifecycleReplacementSlow');
    final gate = Completer<void>();
    backend
      ..initialByAttempt['replacement-close.pdf'] = {2: [nextQuery]}
      ..gates[nextQuery.face] = gate;
    final result = reader.read((document) async {
      if (identical(document, old)) old.missing(firstQuery);
      return identical(document, old) ? 'before-cancellation' : 'replacement';
    });
    try {
      await _until(() => backend.registered.contains(nextQuery.face));
      final replacement = backend.opened['replacement-close.pdf']![1];
      expect(old.disposals, 0);
      expect(replacement.disposals, 0);

      // No native read is pending now: only the new document's resolver is.
      await reader.dispose().timeout(const Duration(seconds: 3));
      expect(gate.isCompleted, isFalse);
      expect(await result, 'before-cancellation');
      expect(old.disposals, 1);
      expect(replacement.disposals, 1);
      expect(replacement.controller.hasListener, isFalse);
      expect(backend.opened['replacement-close.pdf'], hasLength(2));
    } finally {
      gate.complete();
      await _until(() => backend.fontReloads == 2);
    }
  });
}
