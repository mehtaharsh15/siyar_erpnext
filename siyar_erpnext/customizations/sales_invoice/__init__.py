import frappe
from frappe.utils import flt, cint

def validate(self, method):		

	import siyar_erpnext.api
	siyar_erpnext.api.load_customer_item_name(self, method)
	calculate_customer_total(self)


def before_submit(self, method):
	import consoleerp_erpnext_client.customizations.item_stock_validation	
	consoleerp_erpnext_client.customizations.item_stock_validation.validate(self, method)


def on_submit(self, method):
	validate_with_delivery_note(self)

def on_cancel(self, method):
	validate_with_delivery_note(self)

def calculate_customer_total(self):
	total = 0
	for cdoc in self.items:
		if cdoc.consoleerp_customer_rate:
			total += cdoc.consoleerp_customer_rate * cdoc.qty
			cdoc.consoleerp_original_amt = cdoc.consoleerp_customer_rate * cdoc.qty
		else:
			total += cdoc.rate * cdoc.qty

	self.consoleerp_customer_total = total
	self.consoleerp_customer_discount_total = self.consoleerp_customer_total - self.total
	self.consoleerp_order_total = self.total


def validate_with_delivery_note(self):
	# We are only doing this:
	# Make SLEs for changed qtys
	# Make the GL wrt abv
	# make_sl_entries & make_gl_entries handles cancellation

	if self.update_stock == 1:
		# do nothing if updating stock
		return
	sl_entries = []
	changed_rows = []
	# everything wrt stock_qty
	for d in [x for x in self.items if x.validate_with_delivery_note and x.warehouse]:
		if frappe.db.get_value("Item", d.item_code, "is_stock_item") == 1 and flt(d.stock_qty):
			delivered_qty = 0
			if d.dn_detail:
				delivered_qty = frappe.get_value("Delivery Note Item", d.dn_detail, "stock_qty")
			qty_change = d.stock_qty - delivered_qty
			# qty_change
			# -ve	: got return
			# +ve	: gave more
			# 0		: continue
			if qty_change == 0:
				continue

			# return rate- code copied from selling_controller.py
			return_rate = 0
			if cint(self.is_return) and self.return_against and self.docstatus==1:
				return_rate = self.get_incoming_rate_for_sales_return(d.item_code, self.return_against)

			sl_entries.append(self.get_sl_entries(d, {
								"actual_qty": -1*flt(qty_change),
								"incoming_rate": return_rate,
								"parent": "consoleerp-{}".format(self.name)
							}))
			changed_rows.append(d)
	self.make_sl_entries(sl_entries)
	# above method inserts the SLEs
	# stock_value_difference is made only after the above method
	
	# STOCK GL ENTRIES
	# Proceed if perpetual inventory is enabled
	import erpnext
	if not erpnext.is_perpetual_inventory_enabled(self.company):
		return
			
	#--- get stock ledger entries just made
	from erpnext.stock import get_warehouse_account_map
	warehouse_account = get_warehouse_account_map()
	sle_map = {}
	stock_ledger_entries = frappe.db.sql("""
		select
			name, warehouse, stock_value_difference, valuation_rate,
			voucher_detail_no, item_code, posting_date, posting_time,
			actual_qty, qty_after_transaction
		from
			`tabStock Ledger Entry`
		where
			voucher_type=%s and voucher_no=%s and parent=%s
	""", (self.doctype, self.name, "consoleerp-{}".format(self.name)), as_dict=True)

	for sle in stock_ledger_entries:
			sle_map.setdefault(sle.voucher_detail_no, []).append(sle)

	warehouse_with_no_account = []
	gl_list = []
			
	# loop it again
	# stock_controller.get_gl_entries()
	for item_row in changed_rows:
		sle_list = sle_map.get(item_row .name)
		if sle_list:
			for sle in sle_list:
				if warehouse_account.get(sle.warehouse):
					# from warehouse account

					self.check_expense_account(item_row)

					# If the item does not have the allow zero valuation rate flag set
					# and ( valuation rate not mentioned in an incoming entry
					# or incoming entry not found while delivering the item),
					# try to pick valuation rate from previous sle or Item master and update in SLE
					# Otherwise, throw an exception

					if not sle.stock_value_difference and self.doctype != "Stock Reconciliation" \
						and not item_row.get("allow_zero_valuation_rate"):

						sle = self.update_stock_ledger_entries(sle)

					gl_list.append(self.get_gl_dict({
						"account": warehouse_account[sle.warehouse]["account"],
						"against": item_row.expense_account,
						"cost_center": item_row.cost_center,
						"remarks": "Delivery Note Validation Entry",
						"debit": flt(sle.stock_value_difference, 2),
					}, warehouse_account[sle.warehouse]["account_currency"]))

					# to target warehouse / expense account
					gl_list.append(self.get_gl_dict({
						"account": item_row.expense_account,
						"against": warehouse_account[sle.warehouse]["account"],
						"cost_center": item_row.cost_center,
						"remarks": "Delivery Note Validation Entry",
						"credit": flt(sle.stock_value_difference, 2),
						"project": item_row.get("project") or self.get("project")
					}))
				elif sle.warehouse not in warehouse_with_no_account:
					warehouse_with_no_account.append(sle.warehouse)

	if warehouse_with_no_account:
		for wh in warehouse_with_no_account:
			if frappe.db.get_value("Warehouse", wh, "company"):
				frappe.throw(_("Warehouse {0} is not linked to any account, please mention the account in  the warehouse record or set default inventory account in company {1}.").format(wh, self.company))

	from erpnext.accounts.general_ledger import process_gl_map
	gl_list = process_gl_map(gl_list)
	
	from erpnext.accounts.general_ledger import merge_similar_entries
	gl_list = merge_similar_entries(gl_list)
	
	self.make_gl_entries(gl_list)