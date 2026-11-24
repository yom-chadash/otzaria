import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
// ignore: depend_on_referenced_packages
import 'package:pdfrx_engine/pdfrx_engine.dart';

import 'internal_probe.dart' show Observe, fileHash;

Uint8List _fixture(String face) {
  final letters = List.generate(27, (i) => 0x05D0 + i);
  final differences = letters
      .map((u) => '/uni${u.toRadixString(16).toUpperCase().padLeft(4, '0')}')
      .join(' ');
  final content = StringBuffer('BT /F1 36 Tf\n');
  var x = 400;
  for (final rune in 'שלום'.runes) {
    final code = letters.indexOf(rune) + 1;
    x -= 20;
    content.write(
      '1 0 0 1 $x 700 Tm (\\${code.toRadixString(8).padLeft(3, '0')}) Tj\n',
    );
  }
  content.write('ET');
  final objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    '<< /Length ${content.length} >>\nstream\n$content\nendstream',
    '<< /Type /Font /Subtype /TrueType /BaseFont /$face /FirstChar 0 /LastChar 255 '
        '/Widths [${List.filled(256, '500').join(' ')}] '
        '/Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [1 $differences] >> /FontDescriptor 6 0 R >>',
    '<< /Type /FontDescriptor /FontName /$face /Flags 32 /FontBBox [0 -250 1000 850] '
        '/ItalicAngle 0 /Ascent 750 /Descent -250 /CapHeight 700 /StemV 80 >>',
  ];
  final result = StringBuffer('%PDF-1.4\n');
  final offsets = <int>[];
  for (var i = 0; i < objects.length; i++) {
    offsets.add(result.length);
    result.write('${i + 1} 0 obj\n${objects[i]}\nendobj\n');
  }
  final xref = result.length;
  result.write('xref\n0 ${objects.length + 1}\n0000000000 65535 f \n');
  for (final offset in offsets) {
    result.write('${offset.toString().padLeft(10, '0')} 00000 n \n');
  }
  result.write(
    'trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n$xref\n%%EOF\n',
  );
  return Uint8List.fromList(latin1.encode(result.toString()));
}

Future<Map<String, Object?>> _pageProof(PdfDocument document) async {
  final text = (await document.pages.first.loadStructuredText()).fullText;
  final image = await document.pages.first.render(
    fullWidth: 384,
    fullHeight: 384 * 842 / 595,
  );
  if (image == null) throw StateError('Font fixture did not rasterize');
  try {
    var ink = 0;
    for (var i = 0; i < image.pixels.length; i += 4) {
      if (image.pixels[i] < 128 &&
          image.pixels[i + 1] < 128 &&
          image.pixels[i + 2] < 128) {
        ink++;
      }
    }
    return {
      'text': text,
      'containsHebrewWord': text.contains('שלום'),
      'textSha256': sha256.convert(utf8.encode(text)).toString(),
      'pixelSha256': sha256.convert(image.pixels).toString(),
      'inkPixels': ink,
    };
  } finally {
    image.dispose();
  }
}

class _Resolver extends PdfFontResolver {
  _Resolver(this.font);
  final File font;
  var resolves = 0;
  var payloadLoads = 0;

  @override
  Future<PdfFontResolution?> resolve(
    PdfFontQuery query,
    PdfFontResolveContext context,
  ) async {
    resolves++;
    return PdfFontResolution(
      targetFace: query.face,
      resolvedFace: 'Tinos',
      source: font.uri,
      loadData: ({onProgress}) async {
        payloadLoads++;
        final data = await font.readAsBytes();
        onProgress?.call(loaded: data.length, total: data.length);
        return data;
      },
    );
  }
}

Future<void> fontObservations(
  Directory output,
  File font,
  Observe observe,
) async {
  final fixtures = Directory('${output.path}/font-fixtures')..createSync();
  for (final progressive in [false, true]) {
    for (final sharedFace in [true, false]) {
      await PdfrxEntryFunctions.instance.clearAllFontData();
      final faces = List.generate(
        10,
        (i) =>
            'Issue5538Missing_${progressive ? 'P' : 'E'}_${sharedFace ? 'shared' : i}',
      );
      final files = List.generate(10, (i) {
        final file = File(
          '${fixtures.path}/${progressive}_${sharedFace}_$i.pdf',
        );
        file.writeAsBytesSync(_fixture(faces[i]));
        return file;
      });
      await observe(
        'S02',
        'concurrent-missing-font-events',
        {
          'documents': 10,
          'progressive': progressive,
          'sharedFace': sharedFace,
          'automaticFallback': false,
          'fontRegistrationAttempted': false,
          'scope': 'locked native engine event attribution before any fallback',
        },
        () async {
          final documents = await Future.wait(
            files.map(
              (file) => PdfDocument.openFile(
                file.path,
                useProgressiveLoading: progressive,
              ),
            ),
          );
          final streams = <StreamSubscription<PdfDocumentEvent>>[];
          final events = List.generate(10, (_) => <String>[]);
          try {
            for (var i = 0; i < documents.length; i++) {
              final index = i;
              streams.add(
                documents[i].events.listen((event) {
                  if (event is PdfDocumentMissingFontsEvent) {
                    events[index].addAll(
                      event.missingFonts.map((query) => query.face),
                    );
                  }
                }),
              );
            }
            await Future<void>.delayed(Duration.zero);
            final initial = events.map((e) => List<String>.of(e)).toList();
            await Future.wait(
              documents.map((doc) => doc.loadPagesProgressively()),
            );
            final proof = await Future.wait(documents.map(_pageProof));
            await Future<void>.delayed(Duration.zero);
            return {
              'documentResults': List.generate(
                10,
                (i) => {
                  'index': i,
                  'expectedFace': faces[i],
                  'initialEventFaces': initial[i],
                  'afterExtractionAndRenderFaces': List<String>.of(events[i]),
                  'foreignFixtureFaceCount': events[i]
                      .where((f) => f != faces[i] && faces.contains(f))
                      .length,
                  'additionalNonFixtureFaces': events[i]
                      .where((f) => !faces.contains(f))
                      .toSet()
                      .toList(),
                  'missingFaceReported': events[i].contains(faces[i]),
                  ...proof[i],
                },
              ),
              'fixtureSha256': await Future.wait(files.map(fileHash)),
            };
          } finally {
            for (final stream in streams) {
              await stream.cancel();
            }
            for (final document in documents) {
              await document.dispose();
            }
          }
        },
      );
    }
  }

  await PdfrxEntryFunctions.instance.clearAllFontData();
  final resolver = _Resolver(font);
  final manager = PdfFontManager(resolvers: [resolver]);
  const face = 'Issue5538ConcurrentSameFace';
  const query = PdfFontQuery(
    face: face,
    weight: 400,
    isItalic: false,
    charset: PdfFontCharset.hebrew,
    pitchFamily: 0,
  );
  final files = List.generate(10, (i) {
    final file = File('${fixtures.path}/registered-$i.pdf');
    file.writeAsBytesSync(_fixture(face));
    return file;
  });
  for (final warm in [false, true]) {
    final initialResolves = resolver.resolves;
    final initialLoads = resolver.payloadLoads;
    await observe(
      'S02',
      'concurrent-font-manager',
      {
        'documents': 10,
        'warmRegisteredFace': warm,
        'fontSha256': await fileHash(font),
        'scope':
            'existing PdfFontManager, explicit identical query, bundled Tinos resolver',
        'productFallbackChanged': false,
        'queryCharset': 'hebrew',
      },
      () async {
        final timer = Stopwatch()..start();
        final results = await Future.wait(
          List.generate(10, (_) => manager.loadMissingFonts([query])),
        );
        final loadUs = timer.elapsedMicroseconds;
        final documents = await Future.wait(
          files.map((file) => PdfDocument.openFile(file.path)),
        );
        try {
          final proof = await Future.wait(documents.map(_pageProof));
          return {
            'loadUs': loadUs,
            'resolverCalls': resolver.resolves - initialResolves,
            'payloadLoads': resolver.payloadLoads - initialLoads,
            'loadResults': results
                .map(
                  (r) => {
                    'loaded': r.loaded.length,
                    'unresolved': r.unresolved.length,
                    'failed': r.failed.length,
                    'fontsReloaded': r.fontsReloaded,
                  },
                )
                .toList(),
            'documentResults': proof,
            'allDocumentsContainHebrew': proof.every(
              (r) => r['containsHebrewWord'] == true,
            ),
          };
        } finally {
          for (final document in documents) {
            await document.dispose();
          }
        }
      },
    );
  }
}
