{ lib, constants }:
let
  requiredPaths = constants.inventory.requiredPaths;
  allowedTopLevel = constants.inventory.allowedTopLevel;

  getPath =
    components: value:
    if components == [ ] then
      value
    else
      let
        component = builtins.head components;
        rest = builtins.tail components;
      in
      if builtins.isAttrs value && builtins.hasAttr component value then
        getPath rest value.${component}
      else if builtins.isList value && builtins.match "[0-9]+" component != null then
        let
          index = builtins.fromJSON component;
        in
        if index < builtins.length value then
          getPath rest (builtins.elemAt value index)
        else
          throw "list index is out of bounds"
      else
        throw "path component is missing";

  hasPath =
    dotted: document: (builtins.tryEval (getPath (lib.splitString "." dotted) document)).success;

  missingPaths = document: lib.filter (dotted: !(hasPath dotted document)) requiredPaths;
in
{
  load =
    path:
    let
      document = builtins.fromJSON (builtins.readFile path);
      missing = missingPaths document;
      unknown = lib.subtractLists allowedTopLevel (builtins.attrNames document);
    in
    if !builtins.isAttrs document then
      throw "platform inventory must be a JSON object"
    else if unknown != [ ] then
      throw "platform inventory has unknown values: ${lib.concatStringsSep ", " unknown}"
    else if missing != [ ] then
      throw "platform inventory is missing required values: ${lib.concatStringsSep ", " missing}"
    else
      document;

  inherit requiredPaths;
}
