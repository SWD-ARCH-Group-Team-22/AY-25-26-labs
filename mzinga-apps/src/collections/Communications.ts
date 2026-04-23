import payload from "mzinga";
import { PaginatedDocs } from "mzinga/database";
import { CollectionConfig, TypeWithID } from "mzinga/types";
import { AccessUtils } from "../utils";
import { CollectionUtils } from "../utils/CollectionUtils";
import { MailUtils } from "../utils/MailUtils";
import { MZingaLogger } from "../utils/MZingaLogger";
import { TextUtils } from "../utils/TextUtils";
import { Slugs } from "./Slugs";

const access = new AccessUtils();
const collectionUtils = new CollectionUtils(Slugs.Communications);

const Communications: CollectionConfig = {
  slug: Slugs.Communications,
  access: {
    read: access.GetIsAdmin,
    create: access.GetIsAdmin,
    delete: () => {
      return false;
    },
    update: () => {
      return false;
    },
  },
  admin: {
    ...collectionUtils.GeneratePreviewConfig(),
    useAsTitle: "subject",
    // --- LAB 1: Aggiunto "status" alle colonne di default (Step 3.2) ---
    defaultColumns: ["subject", "status", "tos"],
    group: "Notifications",
    disableDuplicate: true,
    enableRichTextRelationship: false,
  },
  hooks: {
    afterChange: [
      async ({ doc, req, operation }) => {
        // --- LAB 1: Guardia per evitare Loop infiniti (Step 4.2) ---
        // Se lo stato è già pending o sent, non fare nulla ed esci subito.
        if (doc.status === "pending" || doc.status === "sent") {
          return doc;
        }

        // --- LAB 1: Feature Flag per delegare al Worker (Step 4.2) ---
        if (process.env.COMMUNICATIONS_EXTERNAL_WORKER === "true") {
          // Aggiorniamo il documento a "pending" e usciamo subito,
          // sbloccando l'interfaccia utente.
          await payload.update({
            collection: Slugs.Communications,
            id: doc.id,
            data: { status: "pending" },
          });
          return doc;
        }

        // ==========================================================
        // --- VECCHIA LOGICA ORIGINALE (eseguita se flag è false) ---
        // ==========================================================
        const { tos, ccs, bccs, subject, body } = doc;
        for (const part of body) {
          if (part.type !== "upload") {
            continue;
          }
          const relationToSlug = part.relationTo;
          const relatedDoc = await payload.findByID({
            collection: relationToSlug,
            id: part.value.id,
          });
          part.value = {
            ...part.value,
            ...relatedDoc,
          };
        }
        const html = TextUtils.Serialize(body || "");
        try {
          const users = await payload.find({
            collection: tos[0].relationTo,
            where: {
              id: {
                in: tos.map((to) => to.value.id || to.value).join(","),
              },
            },
          });
          const usersEmails = users.docs.map((u) => u.email);
          if (!usersEmails.length) {
            throw new Error("No valid email addresses found for 'tos' users.");
          }
          let cc;
          if (ccs) {
            const copiedusers = await payload.find({
              collection: ccs[0].relationTo,
              where: {
                id: {
                  in: ccs.map((cc) => cc.value.id).join(","),
                },
              },
            });
            cc = copiedusers.docs.map((u) => u.email).join(",");
          }
          let bcc;
          if (bccs) {
            const blindcopiedusers = await payload.find({
              collection: bccs[0].relationTo,
              where: {
                id: {
                  in: bccs.map((bcc) => bcc.value.id).join(","),
                },
              },
            });
            bcc = blindcopiedusers.docs.map((u) => u.email).join(",");
          }
          const promises = [];
          for (const to of usersEmails) {
            const message = {
              from: payload.emailOptions.fromAddress,
              subject,
              to,
              cc,
              bcc,
              html,
            };
            promises.push(
              MailUtils.sendMail(payload, message).catch((e) => {
                MZingaLogger.Instance?.error(`[Communications:err] ${e}`);
                return null;
              }),
            );
          }
          await Promise.all(promises.filter((p) => Boolean(p)));

          // --- LAB 1: Aggiorniamo a "sent" alla fine della vecchia logica (Step 4.2) ---
          await payload.update({
            collection: Slugs.Communications,
            id: doc.id,
            data: { status: "sent" },
          });

          return doc;
        } catch (err) {
          if (err.response && err.response.body && err.response.body.errors) {
            err.response.body.errors.forEach((error) =>
              MZingaLogger.Instance?.error(
                `[Communications:err]
                ${error.field}
                ${error.message}`,
              ),
            );
          } else {
            MZingaLogger.Instance?.error(`[Communications:err] ${err}`);
          }
          
          // Se la vecchia logica fallisce, segniamo come failed
          await payload.update({
            collection: Slugs.Communications,
            id: doc.id,
            data: { status: "failed" },
          });
          
          throw err;
        }
      },
    ],
  },
  fields: [
    // --- LAB 1: Nuovo campo status (Step 3) ---
    {
      name: "status",
      type: "select",
      defaultValue: "pending",
      admin: {
        position: "sidebar",
        readOnly: true, // L'utente non può modificarlo dal pannello
      },
      options: [
        { label: "Pending", value: "pending" },
        { label: "Processing", value: "processing" },
        { label: "Sent", value: "sent" },
        { label: "Failed", value: "failed" },
      ],
    },
    {
      name: "subject",
      type: "text",
      required: true,
    },
    {
      name: "tos",
      type: "relationship",
      relationTo: [Slugs.Users],
      required: true,
      hasMany: true,
      validate: (value, { data }) => {
        if (!value && data.sendToAll) {
          return true;
        }
        if (value) {
          return true;
        }
        return "No to(s) or sendToAll have been selected";
      },
      admin: {
        isSortable: true,
      },
      hooks: {
        beforeValidate: [
          async ({ value, data }) => {
            if (data.sendToAll) {
              const promises = [] as Promise<
                PaginatedDocs<Record<string, unknown> & TypeWithID>
              >[];

              const firstSetOfUsers = await payload.find({
                collection: Slugs.Users,
                limit: 100,
              });
              const pages = firstSetOfUsers.totalPages;
              for (let i = 1; i < pages; i++) {
                promises.push(
                  payload.find({
                    collection: Slugs.Users,
                    limit: 100,
                    page: i,
                  }),
                );
              }
              const allDocs = [firstSetOfUsers]
                .concat(await Promise.all(promises))
                .map((p) => p.docs)
                .flat()
                .map((d) => {
                  return { relationTo: Slugs.Users, value: d.id };
                });
              value = allDocs;
            }
            return value;
          },
        ],
      },
    },
    {
      name: "sendToAll",
      type: "checkbox",
      label: "Send to all users?",
    },
    {
      name: "ccs",
      type: "relationship",
      relationTo: [Slugs.Users],
      required: false,
      hasMany: true,
      admin: {
        isSortable: true,
      },
    },
    {
      name: "bccs",
      type: "relationship",
      relationTo: [Slugs.Users],
      required: false,
      hasMany: true,
      admin: {
        isSortable: true,
      },
    },
    {
      name: "body",
      type: "richText",
      required: true,
    },
  ],
};

export default Communications;