import 'package:path/path.dart' as p;

import 'installed_pdf_darwin_contract.dart';

/// הסימולטור והבינרים המותקנים נבדקים שוב מול simctl בתהליך המארח.
List<String> installedPdfIosModuleErrors(Object? value) {
  if (value is! Map) return ['Missing iOS loaded-image evidence'];
  bool absolute(Object? path) =>
      path is String &&
      p.posix.isAbsolute(path) &&
      p.posix.normalize(path) == path;
  final bundle = value['bundle'];
  final executable = value['executable'];
  final simulator = value['simulator'];
  if (value['source'] != installedPdfDarwinSource ||
      value['pid'] is! int ||
      (value['pid'] as int) <= 0 ||
      !{'ios_arm64', 'ios_x64'}.contains(value['abi']) ||
      !absolute(bundle) ||
      !(bundle as String).endsWith('.app') ||
      !absolute(executable) ||
      p.posix.dirname(executable as String) != bundle ||
      !value.containsKey('configuredPdfrxModule') ||
      value['configuredPdfrxModule'] != null ||
      value['printingBackend'] != installedPdfDarwinPrinting ||
      simulator is! Map ||
      simulator['udid'] is! String ||
      !RegExp(
        r'^[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$',
      ).hasMatch(simulator['udid'] as String) ||
      simulator['model'] is! String ||
      !RegExp(
        r'^iPhone[0-9]+,[0-9]+$',
      ).hasMatch(simulator['model'] as String) ||
      !absolute(simulator['runtimeRoot']) ||
      !(simulator['runtimeRoot'] as String).endsWith(
        '.simruntime/Contents/Resources/RuntimeRoot',
      )) {
    return [
      'Invalid iOS Simulator identity, process loader or printing backend',
    ];
  }
  final runtimeRoot = simulator['runtimeRoot'] as String;
  final images = value['images'];
  final symbols = value['symbols'];
  if (images is! List ||
      images.isEmpty ||
      images.any((image) => !absolute(image)) ||
      images.toSet().length != images.length ||
      symbols is! Map) {
    return ['Missing iOS dyld images or resolved symbols'];
  }
  final errors = <String>[];
  final pdfiumImages = <String>{};
  for (final entry in installedPdfDarwinSymbols.entries) {
    for (final name in entry.value) {
      final owner = symbols[name];
      if (owner is! Map ||
          !absolute(owner['image']) ||
          !absolute(owner['canonicalImage']) ||
          !images.contains(owner['image']) ||
          ['address', 'imageBase'].any(
            (key) =>
                owner[key] is! String ||
                !RegExp(r'^0x[0-9a-f]*[1-9a-f][0-9a-f]*$').hasMatch(
                  owner[key] as String,
                ),
          )) {
        errors.add('Missing loaded-image owner for $name');
        continue;
      }
      final image = owner['canonicalImage'] as String;
      if (entry.key == 'pdfium') {
        if (image != executable &&
            image != '$executable.debug.dylib' &&
            !p.posix.isWithin(p.posix.join(bundle, 'Frameworks'), image)) {
          errors.add('$name is not provided by the installed app: $image');
        }
        pdfiumImages.add(image);
      } else if (owner['image'] != image ||
          !['', runtimeRoot].any(
            (root) => ['Frameworks', 'PrivateFrameworks'].any(
              (kind) => p.posix.isWithin('$root/System/Library/$kind', image),
            ),
          )) {
        errors.add(
          '$name is not provided by an Apple system framework: $image',
        );
      }
    }
  }
  if (pdfiumImages.length != 1) {
    errors.add('PDFium entry points must come from one installed app image');
  }
  final expected = {executable, ...pdfiumImages};
  final observed = <String>{};
  final files = value['bundledFiles'];
  if (files is! List) {
    errors.add('Missing installed binary hashes');
  } else {
    for (final file in files) {
      if (file is! Map ||
          file['path'] is! String ||
          !expected.contains(file['path']) ||
          !observed.add(file['path'] as String) ||
          file['bytes'] is! int ||
          (file['bytes'] as int) <= 0 ||
          file['sha256'] is! String ||
          !RegExp(r'^[0-9a-f]{64}$').hasMatch(file['sha256'] as String)) {
        errors.add('Invalid, duplicate or foreign installed binary hash');
      }
    }
    if (observed.length != expected.length || !observed.containsAll(expected)) {
      errors.add('Executable and PDFium image hashes are required');
    }
  }
  return errors;
}
