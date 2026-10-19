export type Field = {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  label: string | null;
  required: boolean;
  default: string | number | boolean | null;
};
export type Entity = { name: string; label: string | null; fields: Field[] };
export type AppConfig = { name: string; local: boolean; entities: Entity[] };
export type Row = {
  id: number;
  [key: string]: string | number | boolean | null;
};
export type Page = {
  items: Row[];
  total: number;
  limit: number;
  offset: number;
};
export type Values = Record<string, string | number | boolean | null>;
export type Query = {
  search: string;
  sort: string;
  descending: boolean;
  limit: number;
  offset: number;
};
