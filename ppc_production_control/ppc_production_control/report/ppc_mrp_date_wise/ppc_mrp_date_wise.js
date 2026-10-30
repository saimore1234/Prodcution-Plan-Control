frappe.query_reports["PPC MRP Date Wise"] = {
	filters: [
		{ fieldname: "run", label: __("MRP Run"), fieldtype: "Link", options: "PPC MRP Run", description: __("Empty = the latest calculated run") },
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
		{ fieldname: "item_code", label: __("Item"), fieldtype: "Link", options: "Item" },
		{ fieldname: "warehouse", label: __("Warehouse"), fieldtype: "Link", options: "Warehouse" },
		{ fieldname: "action", label: __("Action"), fieldtype: "Select", options: "\nPurchase\nManufacture\nReview" },
		{ fieldname: "from_date", label: __("Required From"), fieldtype: "Date" },
		{ fieldname: "to_date", label: __("Required Until"), fieldtype: "Date" },
		{ fieldname: "only_shortages", label: __("Only shortages"), fieldtype: "Check", default: 1 },
	],
};
