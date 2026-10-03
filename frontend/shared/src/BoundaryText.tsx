import { Fragment } from "react";

export function BoundaryText({ text }: { text: string }) {
  return (
    <>
      {text.split(/([/.])/).map((part, index) => (
        <Fragment key={index}>
          {part}
          {(part === "/" || part === ".") && <wbr />}
        </Fragment>
      ))}
    </>
  );
}
