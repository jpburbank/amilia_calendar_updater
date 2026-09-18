# Amilia: Program / Category / Subcategory / Activity hierarchy

Research notes for a coding agent. Compiled 2026-09-18.

Confidence labels used below:

- **DOC**: stated in Amilia's published documentation (URLs in the Sources section).
- **USER**: observed directly by the user in their own Amilia admin.
- **UNVERIFIED**: not confirmed anywhere. Do not treat as fact. See the "Unverified / open questions" section.

---

## 1. Summary

- The hierarchy is fixed at four levels: **Program > Category > Subcategory > Activity**. (DOC, Glossary)
- Only **Program** is a first-class object with its own admin screen, REST endpoints and webhooks.
- **Category** and **Subcategory** have IDs and names, but the docs list **no endpoint** and **no webhook** for them. They only appear as attributes on activities (and on registration/wait list/facility-booking payloads that embed activity info).
- In the admin UI, the Category and Subcategory fields on the Activity form are **a picker of existing values plus free text to create a new value**. (USER)
- Because there are no parent pointers anywhere, the tree must be **derived by grouping activities** on `(ProgramId, CategoryId, SubCategoryId)`.
- Webhooks only deliver changes. An initial load or backfill must come from the REST API.

---

## 2. Data model

```
Program        own object: Id, Name, Start/End, Url, PictureUrl, IsArchived, IsVisible, IsPasswordProtected
└─ Category    Id + Name (no standalone object/endpoint in docs)
   └─ SubCategory   Id + Name (same)
      └─ Activity   Id, Name (the leaf: the actual class/offering)
```

- Glossary definition: a program is a container of similar activities. Amilia's "activity" is what other systems call a program (a class or specific offering, e.g. "Yoga Tuesdays at 7PM"). (DOC)
- Glossary marks Category "(if any)" and SubCategory "(if any)". Sample webhook payloads show `SubCategoryId: 0` and null names. **Treat category and subcategory as nullable.** (DOC)
- An older help article (support.amilia.com, "Creating an activity") says Category, Sub-Category and Name are mandatory for the store to display the activity. Current help says clients drill down 4 levels in the store. The API docs do not enforce non-null. (DOC, conflicting)
- Store drill-down order for clients: Program, then Category, then Subcategory, then activity name. (DOC)
- **League / team-season programs** relabel the two fields: "Division" = category, "Sub Division" = subcategory. Same underlying fields. (DOC)
- **Activity `Groups`** (`{Id, Name}` list) and registration `Group` are not part of the hierarchy. Do not confuse them with Category.
- **Locations are a different tree.** Location payloads carry `ParentId`, `TopParentId`, `AncestorIds`. That is the only true parent/child structure with explicit parent fields in the API. It is unrelated to the program hierarchy. (DOC)
- Activity `Status` values: `Normal`, `Hidden`, `Cancelled`. Program `IsArchived` / `IsVisible` exist on programs. (DOC)

---

## 3. How categories and subcategories get created (admin UI)

1. Activities > Edit, choose a program.
2. Operations > **+ New Activity**.
3. In the form, set **Category**, **Subcategory** and **Name of the activity** (name limit: 100 characters).
   - Picker of existing values plus free text to create a new one. (USER)
   - A category/subcategory value comes into existence when an activity uses it. There is no separate "create category" screen in the help docs.

Other UI facts (DOC):

- **Programs** are created separately: Activities > Programs > **+ New Program**. Activities can only be created inside an existing program.
- **Mass Edit**: select 2 or more activities in Activities > Edit, then Operations > Mass Edit. Editable fields include Category, Subcategory, Name, Billing label, Ledger code, Tags, Status, Forms and others. Schedules, Session, Drop-ins and Payments cannot be mass edited.
- **Duplicate**: Operations > Duplicate activities. The copy appears under the original with an asterisk.
- **Billing label** (optional): if blank, invoices show the program, category, subcategory and activity names.
- **Ordering**: in Activities > Edit, activities, categories and subcategories can be dragged to reorder how they appear in the store. This is from a 2019 article, so it may be outdated.
- **Franchisees** cannot create programs or activities. They pull activities from the franchise's Warehouse.
- **Network setups**: a parent location can create network programs and activity templates. It can allow child locations to edit fields including names, categories and subcategories.
- **Reports**: an "Activities configuration export" report (Reports > Activities) lists activity settings, including subcategory name.
- **API**: in the V3 org reference, the only write endpoints listed are for webhooks (create/delete). No documented endpoint creates programs, categories or activities.

---

## 4. Webhooks

### 4.1 Delivery (DOC)

- HTTP POST to your URL. Expects HTTP 200 as soon as possible.
- On error, retried at a regular interval for roughly 72 hours, after which the subscription is disabled and queued messages are discarded.
- Subscribe via the API management UI (admin settings; the Webhooks module must be enabled) or via the webhooks API endpoint. A subscription needs a name, a URL and a context/action.

Envelope, common to all webhooks:

```json
{
  "OrganizationId": 123,
  "Context": "Activity",
  "Action": "Update",
  "Name": "Activity Update",
  "EventTime": "2022-09-01T14:41:03.548669-04:00",
  "Payload": { }
}
```

Activity webhooks additionally carry `"BillingLabel"` at the envelope level.

### 4.2 Where hierarchy data appears

| Context | Actions | Hierarchy content |
|---|---|---|
| **Activity** | Create, Update | Flat on `Payload`: `ProgramId`, `ProgramName`, `CategoryId`, `CategoryName`, `SubCategoryId`, `SubCategoryName`, plus `Id`, `Name`. **Primary source.** |
| **Activity** | Delete | `Payload: { "Id": "69" }` only (Id is a string) |
| **Registration** | Create, Update, Delete | Nested objects, each `{ "Id", "Name" }`: `Program`, `Category`, `SubCategory`, `Activity`, `Group`. Delete carries the full payload with `IsCancelled: true`. |
| **WaitListRegistration** | Create, Update | Nested `Program`, `Category`, `SubCategory`, `Activity` (each `{Id, Name}`). Delete: `Payload: { "Id": "69" }` only. |
| **FacilityBooking** | Create, Update | `Payload.Activity` block with flat `Id`, `Name`, `ProgramId`, `ProgramName`, `CategoryId`, `CategoryName`, `SubCategoryId`, `SubCategoryName`, `Url`, `Status`. Populated for activity bookings. Delete: `Payload: { "Id": "AB-/CR-/RC-/FB-345" }` only. |
| **Program** | Create, Update, Delete | Program only: `Id`, `Name`, `Url`, `Online`, `StartDate`, `ExpirationDate`. Delete fires when a program is **archived** and carries `Id` only. **No categories, no child activities.** |
| **MerchandisePurchased** | Create, Update | `LinkedPurchase: { "Type": "Activity", "Id": ... }`. An activity ID reference only, so it needs a lookup for hierarchy. |

There is **no** Category or Subcategory webhook context. Webhook contexts documented: Program, Account, Person, FacilityBooking, MembershipPurchased, MerchandisePurchased, MultiPassPurchased, DonationPurchased, Activity, Registration, WaitListRegistration, Alert, Block.

### 4.3 Payload excerpts (exact field names)

Activity Create (hierarchy-relevant fields; sample values as published):

```json
"Payload": {
  "Id": 1,
  "Name": "Test activity",
  "ProgramId": 0,
  "ProgramName": null,
  "CategoryId": 2,
  "CategoryName": "Test Category",
  "SubCategoryId": 0,
  "SubCategoryName": null,
  "Url": null,
  "Status": "Normal"
}
```

Other Activity payload fields: `Tags`, `Description`, `Prerequisite`, `Note`, `ThirdPartyUrl`, `AdditionalInformation`, `ResponsibleName`, `Price`, `DropInPrice`, `DisplayOrder`, `Age {Max, Min, Months}`, `MaxAttendance`, `SpotsRemaining`, `SpotsReserved`, `NumberOfOccurrences`, `StartDate`, `EndDate`, `ScheduleSummary`, `HasSessionEnabled`, `HasDropInEnabled`, `AgeSummary`, `Keywords`, `Groups`, `RegistrationPeriods`, `LocationLabel`, `SecretUrl`, `PictureUrl`. Update additionally has `HasScheduleChanged` and `Forms`.

Registration (Create/Update/Delete):

```json
"Payload": {
  "RegistrationId": "SUB-/DI-/PL-456",
  "Program":     { "Id": 1, "Name": "Test Program" },
  "Activity":    { "Id": 2, "Name": "Test Activity" },
  "Category":    { "Id": 3, "Name": "Test Category" },
  "SubCategory": { "Id": 4, "Name": "Test SubCategory" },
  "Group":       { "Id": 5, "Name": "Test Group" },
  "DropIn": { "OccurrenceId": 6, "OccurrenceDate": "..." },
  "DateCreated": "...",
  "Person": { "Id": 1, "...": "..." },
  "AccountOwner": { "AccountId": 7, "AccountOwnerPersonId": 8 },
  "Staff": { "Id": 2, "FirstName": "Staff", "LastName": "Member", "State": "Normal" },
  "InvoiceItem": { "Id": 35374222 },
  "IsCancelled": false
}
```

ID prefixes:

- Registration IDs: `SUB-` subscription, `DI-` drop-in, `PL-` private lesson.
- FacilityBooking IDs: `AB-` admin booking, `AC-` activity, `PL-` private lesson, `FB-` facility booking, `RC-` rental contract, `CR-` client reservation.

Program webhook:

```json
"Payload": {
  "Id": 1, "Name": "Test Program", "Url": "//www.google.com", "Online": false,
  "StartDate": "2022-08-25T14:41:03.5283954-04:00",
  "ExpirationDate": "2022-09-08T14:41:03.5283954-04:00"
}
```

---

## 5. REST API (v3 org)

- Base URL: `https://app.amilia.com/api/v3/{language}/` where `language` is `en` or `fr`.
- `orgIdentifier`: the org number (e.g. `8008`) or the URL identifier ("rewrite URL", e.g. `forest-explorers`).
- Auth: `GET api/V3/authenticate` (no `{language}` segment) returns `{"Token": "..."}` used on later calls. The docs mention a token or an api-key pair. They recommend a dedicated API user account not tied to one person.
- Errors: 400 bad request, 401 invalid token, 403 not authorized, 404 not found.
- Paging: `page` (default 1) and `perPage` (default 200) parameters. The Use cases samples show a wrapper `{"Items": [...], "Paging": {"TotalCount": n, "Next": ""}}`. The reference page samples show bare arrays or objects in places. Verify actual response shape.
- The v2 API is deprecated. v3 is recommended.

Endpoints relevant to the hierarchy (from the v3 org table of contents and the Use cases page):

| Endpoint | Notes |
|---|---|
| `GET org/{orgIdentifier}/programs` | Item fields (Use cases sample): `Id`, `Name`, `Start`, `End`, `Url`, `PictureUrl`, `IsArchived`, `IsVisible`, `IsPasswordProtected` |
| `GET org/{orgIdentifier}/programs/{id}` | "Get a program for an organization" |
| `GET org/{orgIdentifier}/programs/{id}/activities` | Each item includes `ProgramId`, `ProgramName`, `CategoryId`, `CategoryName`, `SubCategoryId`, `SubCategoryName`. **The route for enumerating the tree per program.** |
| `GET org/{orgIdentifier}/activities/{id}?showTaxes=` | Single activity with the same hierarchy fields |
| `GET org/{orgIdentifier}/activities?showPastActivities=&page=&perPage=&from=&to=` | Labelled **PARTNER** in the reference, not "must be authorized for org". May not be available to org credentials. |
| `GET org/{orgIdentifier}/events?from=&to=&programId=&...` | Each event has an `Activity` block with the hierarchy fields |
| `GET org/{orgIdentifier}/activities/{id}/waitlist` and `.../activities/waitlist` | Include `ProgramId..SubCategoryName` |
| `GET org/{orgIdentifier}/accounts/{id}/registrations` | Items include `ProgramId`, `ProgramName`, `CategoryId`, `CategoryName`, `SubCategoryId`, `SubCategoryName`, `ActivityId`, `ActivityName`, `GroupId`, `GroupName`, `PersonId`, `DateCreated`, `DropInOccurrenceId`, `DropInDate`, `IsCancelled` |
| Registration section: "Get registration by webhook registration id" | Resolves a webhook `RegistrationId` |
| Webhooks section: get / create / delete webhook | Exact paths not verified |

Also listed but not detailed: persons in a program, extra question answers for a program, a person's extra question answers for a program.

There is no Category endpoint anywhere in the v3 org table of contents.

The Use cases page shows two URL forms for the program's activities: `.../org/{org}/programs/{id}/activities` and `.../api/V3/en/programs/{id}/activities` (no `org`). The reference table of contents names the org-scoped one.

v2 (deprecated) notes: v2 program items have `Online`, `IsVisibleOnAmiliaPages`, `IsTeamSeason`. The v2 activity response includes `SubCategoryPosition`.

---

## 6. Design constraints that follow from the above

1. Key on **IDs, not names**. Category creation is free text, so near-duplicates ("Swimming" and "swimming ") can exist as different categories with different IDs. Names alone fragment the tree.
2. Treat `CategoryId`, `SubCategoryId` and `ProgramId` as **nullable/absent**. Samples show `0` and `null`. Handle `0` and `null` as "level not present".
3. The tree is **derived**: group activities on `(ProgramId, CategoryId, SubCategoryId)`. There are no parent pointers and no Category/Subcategory objects or events.
4. A category or subcategory with **no activities is invisible** in every payload.
5. **Deletes carry only an ID** (Activity Delete, WaitListRegistration Delete, FacilityBooking Delete, Program Delete). Keep local state keyed by activity ID to know which node to update.
6. Webhooks are **deltas only**. Backfill from REST (`programs`, then `programs/{id}/activities`).
7. Until uniqueness scope is confirmed (see below), do not assume `CategoryId` or `SubCategoryId` is unique org-wide. Use the full path as the key, or verify against real org data.
8. Program Delete = **archive**. Whether this also emits events for its activities is not documented.

---

## 7. Unverified / open questions

1. **ID uniqueness scope.** Are `CategoryId` and `SubCategoryId` unique org-wide, per program, or per parent category? The docs don't say. The Use cases sample data reuses the same IDs under different names, so it cannot be used as evidence.
2. **Picker filtering.** Is the Category picker filtered by program? Is the Subcategory picker filtered by the selected category?
3. **Rename and mass-edit events.** What webhooks fire when a category is renamed or a mass edit changes Category/Subcategory? Presumably an Activity Update per affected activity, but this is untested.
4. **Populated values at Create time.** The Activity Create sample shows `ProgramId: 0` and `ProgramName: null`. Is this only sample data, or can a real Create payload lack program info? Reconcile with `GET .../activities/{id}` if needed.
5. **Nullability in practice.** Docs conflict (glossary "if any" vs an older help article saying mandatory). Confirm in the org's real data.
6. **Empty categories.** Does a category persist after its last activity is removed or reassigned?
7. **Ordering.** The UI supports dragging categories and subcategories. v2 exposed `SubCategoryPosition`. No category ordering field was seen in v3 or webhook payloads. `DisplayOrder` exists on activities.
8. **V3 Program section.** The fetch of the v3 org reference was truncated before the Program section. Endpoint parameters and response fields for `programs`, `programs/{id}` and `programs/{id}/activities` above come from the table of contents and the Use cases page, not from the endpoint reference. The agent should fetch that section directly.
9. **`GET org/{orgIdentifier}/activities`** (PARTNER label): confirm whether org credentials can call it.

How to check items 1 and 2 in the admin:

- Open an activity in Program B and see whether the Category picker offers values created under Program A. If yes, category IDs are probably org-wide.
- See whether the Subcategory picker is filtered by the selected Category. If it is, subcategories are probably children of that category. If not, treat a subcategory as an independent value that could appear under several categories.

---

## 8. Superseded information (do not use)

A 2024 third-party review (designtlc.com) claimed there is **no central list of categories** and that previously created categories **do not appear in a dropdown**. The user observed in the current admin that the Category and Subcategory fields are a picker for existing values plus free text for new ones. The review is out of date on this point.

---

## 9. Sources

Amilia API docs:

- https://app.amilia.com/apidocs/ApiDocs/V3Org-2.html (v3 org reference; also https://app.amilia.com/apidocs/ApiDocs/v3org.html)
- https://app.amilia.com/apidocs/ApiDocs/v1webhooks.html
- https://app.amilia.com/apidocs/ApiDocs/Glossary.html
- https://app.amilia.com/apidocs/ApiDocs/Usecases.html
- https://app.amilia.com/apidocs/ApiDocs/v2.html (deprecated)
- https://app.amilia.com/apidocs/ (index: base URL, auth, error codes)

Amilia help center:

- https://help.amilia.com/en/articles/3458634-create-an-activity
- https://help.amilia.com/en/articles/3433901-mass-edit-your-activities
- https://help.amilia.com/en/articles/3433924-change-the-order-of-the-activities-in-your-store
- https://help.amilia.com/en/articles/3430261-create-your-programs
- https://help.amilia.com/en/articles/3430266-league-registrations
- https://help.amilia.com/en/articles/14007265-network-activity-templates-for-the-parent-and-child-locations
- https://help.amilia.com/en/articles/13392091-activities-configuration-export-report
- https://support.amilia.com/hc/en-us/articles/212129966-Creating-an-activity (older; "mandatory" wording)

Superseded:

- https://designtlc.com/archive/amilia-class-registration-software-review/
