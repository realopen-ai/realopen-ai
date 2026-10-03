export interface EditablePersona {
  id: string;
  name: string;
  prompt: string;
}

export function replaceCustomPersona(
  personas: EditablePersona[],
  updated: EditablePersona,
): EditablePersona[] {
  return personas.map((persona) =>
    persona.id === updated.id ? updated : persona,
  );
}

export function removeCustomPersona(
  personas: EditablePersona[],
  id: string,
): EditablePersona[] {
  return personas.filter((persona) => persona.id !== id);
}

export function personaAfterDelete(
  selectedId: string,
  deletedId: string,
): string {
  return selectedId === deletedId ? "friendly" : selectedId;
}
