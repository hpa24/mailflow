## Kategorien
kat1 | focus | Fokus | Tiefgehende fachliche Fragen oder komplexe Anliegen, die volle Aufmerksamkeit erfordern
kat2 | quick-reply | Schnell | Kurze organisatorische Fragen, Terminbestätigungen oder einfache Bestätigungen
kat3 | office | Office | Rechnungen, Buchhaltung, Verträge, geschäftliche Unterlagen und Dokumente
kat4 | info-trash | Info | Newsletter, Werbung, automatische Benachrichtigungen ohne direkten Handlungsbedarf

## Haupt-Kategorisierungsprompt
Klassifiziere diese E-Mail in genau eine der folgenden {n} Kategorien:

{categories_block}

{rules_block}Von: {from_email}
Betreff: {subject}
Inhalt (gekürzt): {body}

Antworte NUR mit dem Kategorie-Slug (z.B. focus, quick-reply, office, info-trash). Kein Satzzeichen, kein Erklärungstext.

## Regelextraktions-Prompt
Leite aus dieser manuellen E-Mail-Korrektur eine allgemeine Regel ab.

E-Mail: Von {from_email}, Betreff: "{subject}", Inhalt: {body_snippet}
Korrekte Kategorie: {category_name}

Schreibe eine einzige kurze Regel (max. 15 Wörter), die auf ähnliche E-Mails zutrifft.
Beispiel: "Rechnungen von Lieferanten gehören zu Office."
Nur die Regel, kein Erklärungstext, kein Anführungszeichen.

## Konsolidierungs-Prompt
Fasse diese {n} Lernregeln für die Kategorie "{category_name}" zu maximal 7 Kernregeln zusammen.
Behalte nur die wichtigsten, allgemeinsten Muster. Eliminiere Duplikate und sehr ähnliche Regeln.
Gib jede Regel auf einer eigenen Zeile aus — keine Nummerierung, kein Bindestrich als Präfix, kein Erklärungstext.

Regeln:
{rules_list}
